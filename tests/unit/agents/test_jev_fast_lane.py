from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from artemis.agents.operator.jev_fast_lane import (
    GateContext,
    build_moves,
    build_state,
    gate,
    jev_streak,
    launchable_apps,
    milestone_text,
    move_tool_call,
)
from artemis.services.jev import ChoiceAnswer, build_client


def answer(choice="tap_7", confidence=0.95):
    return ChoiceAnswer(choice, {choice: confidence}, confidence)


def context(**overrides):
    elements = [{"index": 7, "text": "Settings"}]
    base = GateContext("on", build_moves(elements, {}), elements, [])
    return replace(base, **overrides)


@pytest.mark.parametrize(
    ("choice", "confidence", "overrides", "reason"),
    [
        ("tap_7", 0.9, {}, "accepted"),
        ("tap_7", 0.899, {}, "low_confidence"),
        ("tap_7", float("nan"), {}, "malformed_answer"),
        ("tap_7", float("inf"), {}, "malformed_answer"),
        ("tap_7", 1.1, {}, "malformed_answer"),
        ("tap_7", 0.95, {"mode": "off"}, "mode"),
        ("tap_7", 0.95, {"mode": "shadow"}, "mode"),
        ("escalate", 1.0, {}, "escalate"),
        ("tap_1", 1.0, {}, "unknown_move"),
        ("tap_7", 0.95, {"steps": [{"extra_metadata": {"decision_source": "jev"}}] * 3}, "streak"),
    ],
)
def test_gate_clauses(choice, confidence, overrides, reason):
    decision = gate(answer(choice, confidence), context(**overrides))
    assert decision.reason == reason
    assert decision.taken == (reason == "accepted")


@pytest.mark.parametrize(
    "label",
    [
        "Delete",
        "remove item",
        "PAY",
        "buy",
        "Purchase",
        "send",
        "Submit",
        "Confirm",
        "Uninstall",
        "Reset",
        "Erase",
        "Sign out",
        "Log out",
        "sign-out",
        "log  out",
        "設定",
        "",
        "  ",
        "⚙",
    ],
)
def test_unsafe_targets_escalate(label):
    elements = [{"index": 7, "text": label}]
    decision = gate(answer(), context(indexed_elements=elements))
    assert not decision.taken


def test_unicode_boundary_and_safe_substrings():
    for label in ("café settings", "aéabc", "Buyer guide", "Payment history"):
        assert gate(answer(), context(indexed_elements=[{"index": 7, "text": label}])).taken
    assert not gate(answer(), context(indexed_elements=[{"index": 7, "text": "aéab"}])).taken


@pytest.mark.parametrize(
    "choice,direction", [("scroll_down_reveal_below", "up"), ("scroll_up_reveal_above", "down")]
)
def test_scroll_continuation(choice, direction):
    previous = {"action_taken": [{"action": "swipe", "direction": direction}]}
    assert gate(answer(choice), context(steps=[previous])).taken
    assert not gate(answer(choice), context()).taken
    assert not gate(
        answer(choice), context(steps=[previous, {"action_taken": [{"action": "tap"}]}])
    ).taken
    opposite = "down" if direction == "up" else "up"
    assert not gate(
        answer(choice),
        context(steps=[{"action_taken": [{"action": "swipe", "direction": opposite}]}]),
    ).taken


def test_streak_uses_persisted_source_and_old_rows_break_it():
    fast = {"extra_metadata": {"decision_source": "jev"}}
    assert jev_streak([{}, fast, fast]) == 2
    assert jev_streak([fast, {}]) == 0
    assert jev_streak([fast, {"extra_metadata": {"decision_source": "frontier"}}, fast]) == 1


def test_moves_use_real_indices_and_only_labels():
    moves = build_moves(
        [{"index": 7, "text": "Settings"}, {"index": 9, "text": ""}],
        {"Clock": "com.android.deskclock"},
    )
    assert "tap_7" in moves and "tap_1" not in moves and "tap_9" not in moves
    assert "launch_clock" in moves and "escalate" in moves


def test_launch_resolution_and_active_leaf():
    plan = "- [x] Settings done\n- [/] Open Clock\n  - [/] View alarms"
    assert milestone_text(plan) == "Open Clock\nView alarms"
    assert milestone_text("- [x] All done") == ""
    assert launchable_apps(
        milestone_text(plan),
        {"Clock": "com.android.deskclock", "Settings": "com.android.settings", "Alarm": None},
    ) == {"Clock": "com.android.deskclock"}
    assert launchable_apps("Clockwork", {"Clock": "com.android.deskclock"}) == {}


def test_state_is_json_and_bounded():
    state = json.loads(
        build_state(
            "Open Clock",
            "com.android.settings",
            [str(number) for number in range(70)],
            list(range(8)),
        )
    )
    assert state["current_milestone"] == "Open Clock"
    assert len(state["visible_screen_text"]) == 60
    assert state["recent_actions"] == [4, 5, 6, 7]


@pytest.mark.parametrize(
    "choice,name,args",
    [
        ("tap_7", "click", {"target": 7}),
        ("scroll_down_reveal_below", "swipe", {"direction": "up"}),
        ("scroll_up_reveal_above", "swipe", {"direction": "down"}),
        ("press_back", "press_key", {"key": "BACK"}),
        ("press_home", "press_key", {"key": "HOME"}),
        ("launch_clock", "manage_app", {"action": "launch", "app_name": "Clock"}),
    ],
)
def test_tool_calls(choice, name, args):
    assert move_tool_call(choice, {"Clock": "com.android.deskclock"}) == {
        "name": name,
        "args": args,
    }


def test_fast_lane_client_is_separate_from_guardrail():
    settings = SimpleNamespace(
        ARTEMIS_JEV_ENABLED=False,
        ARTEMIS_JEV_FAST_LANE="on",
        TYPESAFE_API_KEY="test",
        TYPESAFE_BASE_URL="https://openrouter.ai/api/v1",
        ARTEMIS_JEV_MODEL="jev-latest",
        ARTEMIS_JEV_FAST_LANE_MODEL="jev-1.13",
    )
    assert build_client(settings) is None
    client = build_client(settings, fast_lane=True)
    assert client is not None and client._model == "jev-1.13"
    settings.ARTEMIS_JEV_FAST_LANE = "off"
    assert build_client(settings, fast_lane=True) is None
