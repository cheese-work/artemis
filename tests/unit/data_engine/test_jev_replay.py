"""Offline replay must reconstruct historical inputs and grade only independent truth."""

import json
import os
from pathlib import Path
import sqlite3

import pytest

from artemis.data_engine.jev_replay import (
    label_step,
    load_steps,
    metrics,
    plan_at,
    replay,
    truth_move,
)
from artemis.services.jev import ChoiceAnswer
from artemis.utils.notes import read_note_content, save_note_content, update_note_content
from artemis.utils.plan_grammar import parse_plan


def test_plan_rebuild_and_labels() -> None:
    notes = [
        (
            1.0,
            {"name": "save_note", "args": {"key": "task_plan", "content": "- [/] Open Settings"}},
        ),
        (
            1.1,
            {
                "name": "save_note",
                "args": {"key": "task_plan", "content": "- [/] Open Settings"},
                "result": "Saved",
            },
        ),
        (
            2.0,
            {
                "name": "update_note",
                "args": {"key": "task_plan", "target": "[/]", "replacement": "[x]"},
                "result": "Updated",
            },
        ),
    ]
    at_step = plan_at(notes, 1.5)
    finished = plan_at(notes, 3.0)
    assert at_step == "- [/] Open Settings"
    assert finished == "- [x] Open Settings"
    assert label_step(True, finished, at_step, "frontier") == "unverified"
    assert label_step(False, finished, at_step, "frontier") == "unverified"
    assert label_step(True, at_step, at_step, "frontier") == "unverified"
    assert label_step(True, finished, at_step, "jev") == "excluded"


@pytest.mark.parametrize(
    "target",
    ["- [/] Open Settings", "- [ ] OPEN Settings", "- [/] Open Setting"],
)
def test_note_updates_match_live_tool(tmp_path: Path, target: str) -> None:
    original = "- [/] Open Settings\n"
    replacement = "- [x] Open Settings"
    save_note_content(tmp_path, "task_plan", original)
    update_note_content(tmp_path, "task_plan", target, replacement)
    notes = [
        (
            1.0,
            {
                "name": "save_note",
                "args": {"key": "task_plan", "content": original},
                "result": "Saved",
            },
        ),
        (
            2.0,
            {
                "name": "update_note",
                "args": {"key": "task_plan", "target": target, "replacement": replacement},
                "result": "Updated",
            },
        ),
    ]
    assert plan_at(notes, 1.5) == original
    assert plan_at(notes, 3.0) == read_note_content(tmp_path, "task_plan")


def test_nested_note_traces_apply_each_operation_once() -> None:
    save = {
        "name": "save_note",
        "args": {"key": "task_plan", "content": "- [ ] Open Settings"},
        "result": "Saved",
    }
    update = {
        "name": "update_note",
        "args": {
            "key": "task_plan",
            "target": "- [ ] Open Settings",
            "replacement": "- [/] Open Settings\n  - [/] Launch Settings",
        },
        "result": "Updated",
    }
    notes = [
        (1.0, {**save, "trace_id": "save"}),
        (1.1, {**save, "trace_id": "save-inner", "parent_trace_id": "save"}),
        (2.0, {**update, "trace_id": "update"}),
        (2.1, {**update, "trace_id": "update-inner", "parent_trace_id": "update"}),
        (3.0, {**update, "result": "Failed to update note"}),
    ]
    assert plan_at(notes, 4.0) == update["args"]["replacement"]


@pytest.mark.parametrize(
    "statuses, expected",
    [
        ([], "unverified"),
        (["unchecked"], "unverified"),
        (["inconclusive"], "unverified"),
        (["failed"], "unverified"),
        (["passed"], "verified"),
        (["failed", "passed"], "verified"),
        (["passed", "failed"], "unverified"),
    ],
)
@pytest.mark.parametrize("checkpoint", ["final", "milestone", "other"])
def test_labels_require_milestone_verdicts(
    statuses: list[str], expected: str, checkpoint: str
) -> None:
    plan = "- [/] Open Settings\n  - verify: Settings is visible"
    milestone = parse_plan(plan).active_milestone()
    assert milestone is not None
    ledger = [
        {
            "kind": "verify",
            "item_text": "Settings is visible",
            "when": "on_complete",
            "checkpoint_id": milestone.key if checkpoint == "milestone" else checkpoint,
            "status": status,
        }
        for status in statuses
    ]
    finished = plan.replace("[/]", "[x]")
    assert label_step(True, finished, plan, "frontier", ledger) == (
        "unverified" if checkpoint == "other" else expected
    )
    assert label_step(False, finished, plan, "frontier", ledger) == "unverified"
    assert label_step(True, finished, plan, "jev", ledger) == "excluded"


def test_missing_verify_criteria_do_not_grade_a_completed_milestone() -> None:
    plan = "- [/] Open Settings"
    ledger = [{"kind": "verify", "item_text": "Unrelated", "status": "passed"}]
    assert label_step(True, plan.replace("[/]", "[x]"), plan, "frontier", ledger) == "unverified"


def test_load_steps_reads_verdicts_and_preserves_empty_app_map(tmp_path: Path) -> None:
    session = "01234567-1111-2222-3333-444444444444"
    plan = "- [/] Open Settings app\n  - verify: Settings is visible"
    db_path = tmp_path / "data_engine.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            "CREATE TABLE sessions(session_id TEXT, start_time REAL, initial_goal TEXT, "
            "status TEXT); CREATE TABLE steps(session_id TEXT, step_id TEXT, "
            "step_number INTEGER, timestamp REAL, action_taken TEXT, extra_metadata TEXT, "
            "pre_image_name TEXT); CREATE TABLE traces(session_id TEXT, step_id TEXT, "
            "name TEXT, timestamp REAL, payload TEXT); "
            "CREATE TABLE images(image_name TEXT, ui_tree TEXT);"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, 0, 'Open Settings app', 'completed')", (session,)
        )
        for number in (1, 2):
            connection.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (
                    session,
                    str(number),
                    number,
                    float(number + 1),
                    json.dumps([{"action": "press_key", "keycode": "KEYCODE_HOME"}]),
                    '{"decision_source":"frontier"}',
                ),
            )
        for timestamp, content in ((1.0, plan), (4.0, plan.replace("[/]", "[x]"))):
            connection.execute(
                "INSERT INTO traces VALUES (?, NULL, 'save_note', ?, ?)",
                (
                    session,
                    timestamp,
                    json.dumps(
                        {"args": {"key": "task_plan", "content": content}, "result": "Saved"}
                    ),
                ),
            )
        for step, apps in (("1", {"Settings": "com.android.settings"}), ("2", {})):
            connection.execute(
                "INSERT INTO traces VALUES (?, ?, 'fast_lane', 2.5, ?)",
                (session, step, json.dumps({"launchable_apps": apps})),
            )
    (tmp_path / "web_01234567_PASS_2026-09-27").mkdir()
    run = tmp_path / session
    run.mkdir()
    ledger = run / "check_ledger.jsonl"
    verdict = {
        "kind": "verify",
        "item_text": "Settings is visible",
        "when": "on_complete",
        "checkpoint_id": "final",
        "status": "unchecked",
    }
    for status, expected in (("unchecked", "unverified"), ("passed", "verified")):
        ledger.write_text(json.dumps({**verdict, "status": status}) + "\n", encoding="utf-8")
        before = {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            for path in tmp_path.iterdir()
        }
        rows = load_steps(db_path)
        assert [row["label"] for row in rows] == [expected, expected]
        assert rows[0]["apps"] == {"Settings": "com.android.settings"}
        assert rows[1]["apps"] == {}
        assert before == {
            path.name: (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
            for path in tmp_path.iterdir()
        }
    with pytest.raises(ValueError, match="empty"):
        load_steps(db_path, [])


def test_truth_and_metrics() -> None:
    elements = [{"index": 25, "text": "About phone", "bounds": [10, 20, 100, 120]}]
    assert (
        truth_move(
            [{"action": "tap", "target_text": "About phone", "target_bounds": [10, 20, 100, 120]}],
            elements,
        )
        == "tap_25"
    )
    assert (
        truth_move([{"action": "swipe", "coordinates": [1, 100, 1, 10]}], elements)
        == "scroll_down_reveal_below"
    )
    assert truth_move([{"action": "tap", "target_text": "Unknown"}], elements) is None
    rows = [
        {
            "label": "verified",
            "truth": "tap_25",
            "choice": "tap_25",
            "taken": True,
            "latency_s": 0.2,
        },
        {
            "label": "verified",
            "truth": "press_back",
            "choice": "press_home",
            "taken": True,
            "latency_s": 0.4,
        },
        {"label": "verified", "truth": "tap_25", "choice": None, "taken": False, "latency_s": 0.6},
        {"label": "excluded", "truth": "tap_25", "choice": None, "taken": False, "latency_s": None},
    ]
    result = metrics(rows, 0.00001)
    assert result["graded"] == 3
    assert result["fast_lane_pct"] == pytest.approx(200 / 3)
    assert result["confident_wrong_pct"] == 50
    assert result["per_move_type"]["tap"]["accuracy_pct"] == 100
    assert result["latency_p90_s"] == pytest.approx(0.56)
    assert result["estimated_cost_usd"] == pytest.approx(0.00003)


@pytest.mark.asyncio
async def test_replay_excludes_jev_and_uses_shared_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answer(*_args: object) -> dict:
        return {
            "next_move": ChoiceAnswer(
                choice="scroll_down_reveal_below", confidence=0.95, probabilities={}
            )
        }

    monkeypatch.setattr("artemis.data_engine.jev_replay.ask", answer)
    state = '{"current_milestone": "Scroll down"}'
    row = {
        "session": "one",
        "step": 1,
        "label": "verified",
        "truth": "scroll_down_reveal_below",
        "state": state,
        "elements": [],
        "apps": {},
        "history": [],
    }
    excluded = {**row, "step": 2, "label": "excluded"}
    result = await replay([row, excluded], object(), 0.9, 2, 0.00001)
    assert result["metrics"]["calls"] == 2
    assert result["metrics"]["fast_laned"] == 0
    assert result["steps"][0]["reason"] == "scroll_continuation"
    assert result["steps"][2]["reason"] == "not_graded"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice, wrong", [("tap_1", 1), ("escalate", 0)])
async def test_text_input_is_graded_as_escalation(
    monkeypatch: pytest.MonkeyPatch, choice: str, wrong: int
) -> None:
    async def answer(*_args: object) -> dict:
        return {"next_move": ChoiceAnswer(choice=choice, confidence=0.99, probabilities={})}

    monkeypatch.setattr("artemis.data_engine.jev_replay.ask", answer)
    elements = [{"index": 1, "text": "Search", "bounds": [0, 0, 100, 50]}]
    row = {
        "session": "one",
        "step": 1,
        "label": "verified",
        "truth": truth_move([{"action": "focus_and_input_text", "text": "Settings"}], elements),
        "state": '{"current_milestone":"Enter Settings in Search"}',
        "elements": elements,
        "apps": {},
        "history": [],
    }
    report = await replay([row], object(), 0.9, 1, 0.00001)
    assert row["truth"] == "escalate"
    assert report["metrics"]["calls"] == 1
    assert report["metrics"]["graded"] == 1
    assert report["metrics"]["confident_wrong"] == wrong
    assert report["metrics"]["fast_lane_pct"] == 100 * wrong
    if wrong:
        assert report["metrics"]["per_move_type"]["escalate"]["accuracy_pct"] == 0


def test_frontier_only_truth_is_distinct_from_ambiguous_recordings() -> None:
    assert truth_move([{"action": "long_press_on"}], []) == "escalate"
    assert truth_move([{"action": "press_key", "keycode": "KEYCODE_ENTER"}], []) == "escalate"
    assert truth_move([{"action": "swipe", "coordinates": [1, 10, 100, 10]}], []) == "escalate"
    assert truth_move([{"action": "tap", "target_text": "Missing"}], []) is None
    assert truth_move([{"action": "unrecognized"}], []) is None


@pytest.mark.parametrize("selector", [",", " , , ", "   "])
def test_empty_session_selector_is_rejected_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    from artemis.interfaces.cli.commands import jev as command
    import typer
    from typer.testing import CliRunner

    def load(*_args: object) -> list:
        pytest.fail("An empty selector must not load the trace store")

    monkeypatch.setattr(command, "load_steps", load)
    app = typer.Typer()
    app.add_typer(command.jev_app, name="jev")
    result = CliRunner().invoke(
        app, ["jev", "replay", "--path", str(tmp_path), "--sessions", selector, "--json"]
    )
    assert result.exit_code == 2
    assert "non-empty session" in result.output


def test_real_session_truth_when_store_available() -> None:
    store = Path(os.environ.get("ARTEMIS_REPLAY_TEST_STORE", "")) / "data_engine.db"
    if not store.exists():
        pytest.skip("Local CHE-645 dataset absent")
    rows = load_steps(store, ["dcf43ab9"])
    assert len(rows) == 15
    assert all(row["label"] == "verified" for row in rows)
    assert rows[0]["truth"] == "launch_settings"
    assert rows[4]["truth"] == "tap_25"
