"""Independent probe: a production checkpoint repair must retain its milestone."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from artemis.agents.operator.jev_fast_lane import milestone_text
from artemis.data_engine.jev_replay import _reconstruct_plan, load_steps, replay
from artemis.graph.checkpoints import revert_subgoal_status
from artemis.utils.notes import read_note_content, save_note_content, update_note_content
from artemis.utils.plan_grammar import parse_plan


@pytest.mark.parametrize("advance_clock", [False, True])
@pytest.mark.parametrize("save_reopened_plan", [False, True])
@pytest.mark.parametrize("failure_timestamp", [2.5, None, float("nan")])
def test_reopened_checkpoint_uses_live_milestone(
    tmp_path, advance_clock, save_reopened_plan, failure_timestamp
):
    sid = "01234567-1111-2222-3333-444444444444"
    run = tmp_path / sid
    run.mkdir()
    original = (
        "- [/] Open Settings app\n"
        "  - verify: Settings is visible\n"
        "- [ ] Open Clock app\n"
        "  - verify: Clock is visible\n"
    )
    snapshot = parse_plan(original)
    settings, clock = snapshot.top_level
    notes = []

    save_note_content(run, "task_plan", original)
    notes.append(
        (
            1.0,
            "save_note",
            {
                "args": {"key": "task_plan", "content": original},
                "result": "Saved note 'task_plan'.",
            },
        )
    )

    def update(when, target, replacement):
        update_note_content(run, "task_plan", target, replacement)
        notes.append(
            (
                when,
                "update_note",
                {
                    "args": {"key": "task_plan", "target": target, "replacement": replacement},
                    "result": "Updated note 'task_plan'.",
                },
            )
        )

    update(2.0, "- [/] Open Settings app", "- [x] Open Settings app")
    if advance_clock:
        update(2.1, "- [ ] Open Clock app", "- [/] Open Clock app")
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=run))
    assert revert_subgoal_status(ctx, settings.key)
    actual_at_repair = milestone_text(read_note_content(run, "task_plan"))
    assert actual_at_repair == "Open Settings app"
    if save_reopened_plan:
        reopened = read_note_content(run, "task_plan")
        save_note_content(run, "task_plan", reopened)
        notes.append(
            (
                2.6,
                "save_note",
                {
                    "args": {"key": "task_plan", "content": reopened},
                    "result": "Saved note 'task_plan'.",
                },
            )
        )
    update(4.0, "- [/] Open Settings app", "- [x] Open Settings app")
    update(
        5.0,
        "- [/] Open Clock app" if advance_clock else "- [ ] Open Clock app",
        "- [x] Open Clock app",
    )
    ledger = [
        {
            "ts": when,
            "kind": "verify",
            "when": "on_complete",
            "checkpoint_id": milestone.key,
            "item_text": text,
            "status": status,
        }
        for when, milestone, text, status in (
            (failure_timestamp, settings, "Settings is visible", "failed"),
            (4.5, settings, "Settings is visible", "passed"),
            (5.5, clock, "Clock is visible", "passed"),
        )
    ]
    (run / "check_ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ledger))
    (tmp_path / "web_01234567_PASS_2026-09-27").mkdir()
    db = tmp_path / "data_engine.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            "CREATE TABLE sessions(session_id TEXT, start_time REAL, initial_goal TEXT, status TEXT);"
            "CREATE TABLE steps(session_id TEXT, step_id TEXT, step_number INTEGER, timestamp REAL, action_taken TEXT, extra_metadata TEXT, pre_image_name TEXT);"
            "CREATE TABLE traces(session_id TEXT, step_id TEXT, name TEXT, timestamp REAL, payload TEXT);"
            "CREATE TABLE images(image_name TEXT, ui_tree TEXT);"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, 0, 'Open Settings app then Clock app', 'completed')",
            (sid,),
        )
        connection.execute(
            "INSERT INTO steps VALUES (?, 'repair', 1, 3, ?, ?, NULL)",
            (
                sid,
                json.dumps([{"action": "launch_app", "app_name": "Settings"}]),
                '{"decision_source":"frontier"}',
            ),
        )
        for timestamp, name, payload in notes:
            connection.execute(
                "INSERT INTO traces VALUES (?, NULL, ?, ?, ?)",
                (sid, name, timestamp, json.dumps(payload)),
            )
    (row,) = load_steps(db, [sid])
    observed = {
        "actual_milestone": actual_at_repair,
        "replay_milestone": json.loads(row["state"])["current_milestone"],
        "apps": row["apps"],
        "label": row["label"],
        "truth": row["truth"],
    }
    print(json.dumps(observed, sort_keys=True))
    dated = failure_timestamp == 2.5
    if dated or save_reopened_plan:
        assert observed["replay_milestone"] == actual_at_repair, observed
        assert observed["apps"] == {"Settings": "Settings"}
    assert observed["truth"] == "launch_settings"
    assert observed["label"] == ("verified" if save_reopened_plan and dated else "unverified")
    assert row["plan_reconstruction"] == (
        "undated_checkpoint_verdict"
        if not dated
        else "recorded"
        if save_reopened_plan
        else "inferred_checkpoint_reopen"
    )


@pytest.mark.parametrize("label", ["unverified", "excluded"])
@pytest.mark.asyncio
async def test_uncertain_repairs_are_retained_without_querying_jev(monkeypatch, label):
    async def ask(*_args):
        pytest.fail("An inferred checkpoint state must not be sent to Jev")

    monkeypatch.setattr("artemis.data_engine.jev_replay.ask", ask)
    row = {
        "session": "repair",
        "step": 3,
        "label": label,
        "truth": "launch_settings",
        "state": '{"current_milestone":"Open Settings app"}',
        "plan_reconstruction": "inferred_checkpoint_reopen",
    }
    report = await replay([row], object(), 0.9, 3, 0.00001)
    (result,) = report["steps"]
    assert result["truth"] == "launch_settings"
    assert result["label"] == label
    assert result["reason"] == "unreconstructable_plan"
    assert report["metrics"]["unreconstructable_steps"] == 1
    assert report["metrics"]["graded"] == 0
    assert report["metrics"]["calls"] == 0


@pytest.mark.parametrize("item_text", ["Settings is visible", "Retired criterion"])
def test_inferred_reset_is_timed_and_only_a_full_save_clears_uncertainty(item_text):
    completed = "- [x] Open Settings app\n  - verify: Settings is visible"
    (milestone,) = parse_plan(completed).top_level
    notes = [
        (
            1.0,
            {
                "name": "save_note",
                "args": {"key": "task_plan", "content": completed},
                "result": "Saved",
            },
        ),
        (
            2.0,
            {"name": "checkpoint_reopen", "checkpoint_id": milestone.key, "item_text": item_text},
        ),
        (
            3.0,
            {
                "name": "update_note",
                "args": {
                    "key": "task_plan",
                    "target": "[/]" if item_text == "Settings is visible" else "[x]",
                    "replacement": "[x]",
                },
                "result": "Updated",
            },
        ),
        (
            4.0,
            {
                "name": "save_note",
                "args": {"key": "task_plan", "content": completed},
                "result": "Saved",
            },
        ),
    ]
    assert _reconstruct_plan(notes, 1.5) == (completed, False)
    matching = item_text == "Settings is visible"
    assert _reconstruct_plan(notes, 2.5) == (
        completed.replace("[x]", "[/]") if matching else completed,
        matching,
    )
    assert _reconstruct_plan(notes, 3.5) == (completed, matching)
    assert _reconstruct_plan(notes, 4.5) == (completed, False)


@pytest.mark.parametrize("target", ["Open Settings app", "Unrecorded replacement milestone"])
def test_note_edits_after_a_possible_reset_keep_uncertainty(target):
    completed = "- [x] Open Settings app\n  - verify: Settings is visible"
    (milestone,) = parse_plan(completed).top_level
    notes = [
        (
            1.0,
            {
                "name": "save_note",
                "args": {"key": "task_plan", "content": completed},
                "result": "Saved",
            },
        ),
        (
            2.0,
            {
                "name": "checkpoint_reopen",
                "checkpoint_id": milestone.key,
                "item_text": "Settings is visible",
            },
        ),
        (
            3.0,
            {
                "name": "update_note",
                "args": {
                    "key": "task_plan",
                    "target": f"- [x] {target}",
                    "replacement": "- [x] Retitled milestone",
                },
                "result": "Updated",
            },
        ),
    ]
    plan, uncertain = _reconstruct_plan(notes, 3.5)
    assert uncertain
    assert ("Retitled milestone" in plan) == (target == "Open Settings app")
