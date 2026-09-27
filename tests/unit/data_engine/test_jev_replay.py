"""Offline replay must reconstruct historical inputs and grade only independent truth."""

import os
from pathlib import Path

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
    assert label_step(True, finished, at_step, "frontier") == "verified"
    assert label_step(False, finished, at_step, "frontier") == "unverified"
    assert label_step(True, at_step, at_step, "frontier") == "unverified"
    assert label_step(True, finished, at_step, "jev") == "excluded"


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


def test_real_session_truth_when_store_available() -> None:
    store = Path(os.environ.get("ARTEMIS_REPLAY_TEST_STORE", "")) / "data_engine.db"
    if not store.exists():
        pytest.skip("Local CHE-645 dataset absent")
    rows = load_steps(store, ["dcf43ab9"])
    assert len(rows) == 15
    assert all(row["label"] == "verified" for row in rows)
    assert rows[0]["truth"] == "launch_settings"
    assert rows[4]["truth"] == "tap_25"
