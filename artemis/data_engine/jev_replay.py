"""Offline Jev evaluation against recorded, independently verified Operator actions."""

from collections import Counter, defaultdict
from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from artemis.agents.operator.jev_fast_lane import (
    GateContext,
    build_moves,
    build_state,
    gate,
    launchable_apps,
    milestone_text,
)
from artemis.data_engine.engine import _derive_foreground_app
from artemis.graph.checkpoints import read_ledger, resolve_item_status
from artemis.services.jev import ChoiceAnswer, JevClient, ask, choice_question
from artemis.utils.notes import replace_note_text
from artemis.utils.plan_grammar import parse_plan
from artemis.utils.visualization import format_minimal_list_with_elements


def _json(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def plan_at(notes: list[tuple[float, dict]], timestamp: float) -> str:
    """Apply successful note operations, in timestamp order, up to a step."""
    return _reconstruct_plan(notes, timestamp)[0]


def _reconstruct_plan(notes: list[tuple[float, dict]], timestamp: float) -> tuple[str, bool]:
    """Infer possible checkpoint resets; only a full note save clears uncertainty."""
    plan = ""
    uncertain = False
    operations = {}
    for when, payload in notes:
        if when > timestamp:
            break
        if payload.get("name") == "checkpoint_reopen":
            snapshot = parse_plan(plan)
            checkpoint = payload.get("checkpoint_id")
            if not any(
                check.kind == "verify"
                and check.when == "on_complete"
                and check.parent_key == checkpoint
                and check.text == payload.get("item_text")
                for check in snapshot.check_items
            ):
                continue
            for milestone in snapshot.top_level:
                if milestone.key == checkpoint and milestone.is_done:
                    lines = plan.split("\n")
                    lines[milestone.line_no] = lines[milestone.line_no].replace("[x]", "[/]", 1)
                    plan = "\n".join(lines)
                    uncertain = True
                    break
            continue
        arguments = payload.get("args") or {}
        if arguments.get("key") != "task_plan" or not str(payload.get("result", "")).startswith(
            ("Saved", "Updated")
        ):
            continue
        operation = (payload.get("name"), arguments)
        duplicate = operations.get(payload.get("parent_trace_id")) == operation
        if payload.get("trace_id"):
            operations[payload["trace_id"]] = operation
        if duplicate:
            continue
        if payload.get("name") == "save_note":
            plan = str(arguments.get("content") or "")
            uncertain = False
        elif payload.get("name") == "update_note":
            target = arguments.get("target")
            replacement = arguments.get("replacement")
            if isinstance(target, str) and isinstance(replacement, str):
                try:
                    plan, _ = replace_note_text(plan, "task_plan", target, replacement)
                except ValueError:
                    if not uncertain:
                        raise
    return plan, uncertain


def truth_move(actions: list[dict], elements: list[dict]) -> str | None:
    if len(actions) > 1 and all(truth_move([action], elements) for action in actions):
        return "escalate"
    if len(actions) != 1:
        return None
    action = actions[0]
    kind = action.get("action")
    if kind in ("focus_and_input_text", "long_press_on", "stop_app", "wait_for_delay"):
        return "escalate"
    if kind == "launch_app":
        return f"launch_{str(action.get('app_name', '')).lower()}"
    if kind == "press_key":
        key = str(action.get("keycode") or action.get("key") or "").removeprefix("KEYCODE_")
        if key in ("BACK", "HOME"):
            return f"press_{key.lower()}"
        return "escalate" if key else None
    if kind == "swipe":
        direction = action.get("direction")
        coordinates = action.get("coordinates")
        if (
            direction not in ("up", "down")
            and isinstance(coordinates, list)
            and len(coordinates) >= 4
        ):
            if coordinates[3] == coordinates[1]:
                return "escalate"
            direction = "up" if coordinates[3] < coordinates[1] else "down"
        return {"up": "scroll_down_reveal_below", "down": "scroll_up_reveal_above"}.get(
            str(direction)
        )
    if kind == "tap":
        bounds = action.get("target_bounds")
        matches = [element for element in elements if element.get("bounds") == bounds]
        target = action.get("target_text")
        if target:
            matches = [element for element in matches if element.get("text") == target]
        if len(matches) == 1:
            return f"tap_{matches[0]['index']}"
        coordinates = action.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            matches = [
                element
                for element in elements
                if element.get("text") == target and element.get("center") == coordinates[:2]
            ]
            if len(matches) == 1:
                return f"tap_{matches[0]['index']}"
    return None


def label_step(
    pass_run: bool,
    final_plan: str,
    step_plan: str,
    source: str | None,
    ledger: list[dict] | None = None,
) -> str:
    if source == "jev":
        return "excluded"
    milestone = parse_plan(step_plan).active_milestone()
    if pass_run and source in (None, "frontier") and milestone is not None:
        final = parse_plan(final_plan)
        checks = [
            check
            for check in final.check_items
            if check.kind == "verify" and check.parent_key == milestone.key
        ]
        if (
            any(item.key == milestone.key and item.is_done for item in final.top_level)
            and checks
            and all(
                resolve_item_status(
                    "verify",
                    [
                        record
                        for record in ledger or []
                        if record.get("kind") == "verify"
                        and record.get("item_text") == check.text
                        and record.get("when", "on_complete") == check.when
                        and record.get("checkpoint_id") in (milestone.key, "final")
                    ],
                )
                == "passed"
                for check in checks
            )
        ):
            return "verified"
    return "unverified"


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent
    lower = math.floor(index)
    return ordered[lower] + (ordered[math.ceil(index)] - ordered[lower]) * (index - lower)


def metrics(rows: list[dict], cost_per_call: float) -> dict:
    graded = [row for row in rows if row["label"] == "verified" and row["truth"]]
    taken = [row for row in graded if row["taken"]]
    wrong = sum(row["choice"] != row["truth"] for row in taken)
    by_move = defaultdict(lambda: [0, 0])
    for row in taken:
        move_type = row["truth"].split("_")[0]
        by_move[move_type][0] += 1
        by_move[move_type][1] += row["choice"] == row["truth"]
    latencies = [row["latency_s"] for row in rows if row["latency_s"] is not None]
    calls = len(latencies)
    return {
        "steps": len(rows),
        "labels": dict(Counter(row["label"] for row in rows)),
        "unreconstructable_steps": sum(
            row.get("reason") == "unreconstructable_plan" for row in rows
        ),
        "graded": len(graded),
        "fast_laned": len(taken),
        "fast_lane_pct": 100 * len(taken) / len(graded) if graded else 0.0,
        "confident_wrong": wrong,
        "confident_wrong_pct": 100 * wrong / len(taken) if taken else 0.0,
        "per_move_type": {
            move: {"fast_laned": total, "correct": correct, "accuracy_pct": 100 * correct / total}
            for move, (total, correct) in sorted(by_move.items())
        },
        "latency_p50_s": _percentile(latencies, 0.5),
        "latency_p90_s": _percentile(latencies, 0.9),
        "calls": calls,
        "estimated_cost_usd": calls * cost_per_call,
    }


def load_steps(db_path: Path, sessions: list[str] | None = None) -> list[dict]:
    """Read an existing trace store without creating or mutating its database."""
    if sessions is not None and (not sessions or any(not value.strip() for value in sessions)):
        raise ValueError("Session selection must not be empty")
    if Path(f"{db_path}-wal").exists():
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    with closing(connection):
        connection.row_factory = sqlite3.Row
        stored = connection.execute("SELECT * FROM sessions ORDER BY start_time").fetchall()
        if sessions:
            stored = [
                session
                for session in stored
                if any(session["session_id"].startswith(value) for value in sessions)
            ]
            if not stored:
                raise ValueError(f"No sessions matching: {', '.join(sessions)}")
        records = []
        for session in stored:
            session_id = session["session_id"]
            steps = connection.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number", (session_id,)
            ).fetchall()
            trace_rows = connection.execute(
                "SELECT * FROM traces WHERE session_id = ? "
                "AND (name IN ('save_note', 'update_note', 'fast_lane')) ORDER BY timestamp, rowid",
                (session_id,),
            ).fetchall()
            notes = []
            lanes = {}
            for trace in trace_rows:
                payload = _json(trace["payload"], {})
                if trace["name"] == "fast_lane":
                    if trace["step_id"]:
                        lanes[trace["step_id"]] = payload.get("launchable_apps") or {}
                else:
                    notes.append((trace["timestamp"], {**dict(trace), **payload}))
            ledger = read_ledger(db_path.parent / session_id)
            failed_checks = [
                record
                for record in ledger
                if record.get("kind") == "verify"
                and record.get("status") == "failed"
                and record.get("when", "on_complete") == "on_complete"
                and record.get("checkpoint_id") not in (None, "final")
            ]
            undated_reopen = any(
                not isinstance(record.get("ts"), (int, float)) or not math.isfinite(record["ts"])
                for record in failed_checks
            )
            notes.extend(
                (record["ts"], {**record, "name": "checkpoint_reopen"})
                for record in failed_checks
                if isinstance(record.get("ts"), (int, float)) and math.isfinite(record["ts"])
            )
            notes.sort(key=lambda note: note[0])
            final_plan = plan_at(notes, float("inf"))
            pass_run = session["status"] == "completed" and any(
                "_PASS_" in path.name and session_id[:8] in path.name
                for path in db_path.parent.iterdir()
                if path.is_dir()
            )
            goal_apps = {
                name: name
                for name in re.findall(r"\b([A-Z][\w.-]+)\s+app\b", session["initial_goal"] or "")
            }
            history = []
            for step in steps:
                actions = _json(step["action_taken"], [])
                if not actions:
                    continue
                timestamp = step["timestamp"]
                plan, uncertain = _reconstruct_plan(notes, timestamp)
                reconstruction = (
                    "undated_checkpoint_verdict"
                    if undated_reopen
                    else "inferred_checkpoint_reopen"
                    if uncertain
                    else "recorded"
                )
                milestone = milestone_text(plan)
                metadata = _json(step["extra_metadata"], {})
                source = metadata.get("decision_source")
                image = connection.execute(
                    "SELECT ui_tree FROM images WHERE image_name = ?", (step["pre_image_name"],)
                ).fetchone()
                xml = _json(image["ui_tree"], []) if image else []
                if not isinstance(xml, list):
                    xml = []
                width = metadata.get("width") or 1080
                height = metadata.get("height") or 2400
                _, elements, _ = format_minimal_list_with_elements(xml, width, height)
                apps = lanes.get(step["step_id"], goal_apps)
                apps = launchable_apps(milestone, apps)
                record = {
                    "session": session_id,
                    "step": step["step_number"],
                    "label": label_step(
                        pass_run and reconstruction == "recorded", final_plan, plan, source, ledger
                    ),
                    "plan_reconstruction": reconstruction,
                    "truth": truth_move(actions, elements),
                    "state": build_state(
                        milestone,
                        _derive_foreground_app(xml),
                        [
                            str(node.get("text") or node.get("content-desc"))
                            for node in xml
                            if node.get("text") or node.get("content-desc")
                        ],
                        [action for previous in history for action in previous["action_taken"]][
                            -4:
                        ],
                    ),
                    "elements": elements,
                    "apps": apps,
                    "history": list(history),
                }
                records.append(record)
                history.append({"action_taken": actions, "extra_metadata": metadata})
        return records


async def replay(
    rows: list[dict], client: JevClient, threshold: float, runs: int, cost_per_call: float
) -> dict:
    results = []
    for row in rows:
        unreconstructable = row.get("plan_reconstruction", "recorded") != "recorded"
        if (
            row["label"] == "excluded"
            or not row["truth"]
            or not json.loads(row["state"])["current_milestone"]
            or unreconstructable
        ):
            results.append(
                {key: row[key] for key in ("session", "step", "label", "truth")}
                | {
                    "plan_reconstruction": row.get("plan_reconstruction", "recorded"),
                    "choice": None,
                    "taken": False,
                    "reason": "unreconstructable_plan" if unreconstructable else "not_graded",
                    "latency_s": None,
                }
            )
            continue
        moves = build_moves(row["elements"], row["apps"])
        question = choice_question(
            "Choose the single next move that progresses the current milestone. "
            "If the next move needs text input, completion, recovery, or is unclear, escalate.",
            moves,
        )
        for _ in range(runs):
            started = time.monotonic()
            response = await ask(client, row["state"], {"next_move": question})
            latency = time.monotonic() - started
            candidate = (response or {}).get("next_move")
            answer = candidate if isinstance(candidate, ChoiceAnswer) else None
            decision = gate(
                answer, GateContext("on", moves, row["elements"], row["history"], threshold)
            )
            results.append(
                {
                    "session": row["session"],
                    "step": row["step"],
                    "label": row["label"],
                    "plan_reconstruction": row.get("plan_reconstruction", "recorded"),
                    "truth": row["truth"],
                    "choice": answer.choice if answer else None,
                    "confidence": answer.confidence if answer else None,
                    "taken": decision.taken,
                    "reason": decision.reason,
                    "latency_s": latency,
                }
            )
    return {
        "threshold": threshold,
        "runs": runs,
        "metrics": metrics(results, cost_per_call),
        "steps": results,
    }
