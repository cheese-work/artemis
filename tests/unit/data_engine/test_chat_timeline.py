import pytest

from tests.unit.data_engine.test_chat_sessions import store as store


def test_typed_events_keep_one_order_across_turns_and_restarts(store):
    turn = store.new_turn("legacy", "Check logout", "follow-up")
    identity = turn["turn_id"]
    store.record_event(identity, "plan", "1. Open settings\n2. Log out")
    store.record_event(identity, "thinking", "Check the current screen first")
    store.record_event(
        identity, "step", "Open settings", payload={"step_id": "step-1"}, event_key="step-1"
    )
    store.record_event(identity, "replan", "Use the account menu instead")
    store.record_event(identity, "input_request", "Enter a password", payload={"secret": True})
    store.migrate()
    messages = store.get("legacy")["messages"]
    assert [message["kind"] for message in messages] == [
        "message",
        "message",
        "plan",
        "thinking",
        "step",
        "replan",
        "input_request",
    ]
    assert [message["sequence"] for message in messages] == sorted(
        message["sequence"] for message in messages
    )
    assert messages[4]["payload"] == {"step_id": "step-1"}
    assert all(message["turn_id"] == identity for message in messages[1:])
    assert [message["kind"] for message in store.pending(identity)] == ["message"]


def test_replayed_event_is_idempotent_without_using_human_client_ids(store):
    turn = store.new_turn("legacy", "Check logout", "step-1")
    identity = turn["turn_id"]
    first = store.record_event(identity, "step", "Open settings", event_key="step-1")
    second = store.record_event(identity, "step", "Open settings", event_key="step-1")
    assert first == second
    assert len(store.get(identity)["messages"]) == 3


def test_invalid_event_does_not_write(store):
    before = store.get("legacy")
    with pytest.raises(ValueError, match="invalid_event_kind"):
        store.record_event("legacy", "unknown", "Invalid")
    assert store.get("legacy") == before
