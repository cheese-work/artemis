import asyncio
import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore, TRANSITIONS
from artemis.runtime.chat_control import ChatControl


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run', 'Check login', 'queued', 1, NULL)")
    return ChatStore(path)


@pytest.mark.parametrize(
    "before", ["queued", "running", "paused", "completed", "failed", "cancelled", "interrupted"]
)
@pytest.mark.parametrize(
    "after", ["queued", "running", "paused", "completed", "failed", "cancelled", "interrupted"]
)
def test_every_state_transition(store, before, after):
    with store.transaction() as connection:
        connection.execute("UPDATE chat_turns SET status=? WHERE turn_id=?", (before, "run"))
    if before == after or after in TRANSITIONS[before]:
        store.transition("run", after)
        assert store.turn("run")["status"] == after
    else:
        with pytest.raises(ValueError, match="invalid_transition"):
            store.transition("run", after)
        assert store.turn("run")["status"] == before


@pytest.mark.asyncio
async def test_queued_bubbles_open_without_plan_judgement(store):
    store.send("run", "Check logout", "two")

    async def judge(message):
        pytest.fail("Opening messages must not trigger a plan-change judgement")

    control = ChatControl(store, "run", judge, poll_seconds=0)
    assert await control.checkpoint() == "Check login\n\nCheck logout"
    assert store.pending("run") == []
    assert (
        len([message for message in store.get("run")["messages"] if message["role"] == "agent"])
        == 2
    )


@pytest.mark.asyncio
async def test_fake_loop_steers_and_keeps_goal(store):
    async def judge(message):
        return {"plan_change": False, "reply": "I will use the visible button."}

    control = ChatControl(store, "run", judge, poll_seconds=0)
    await control.checkpoint()
    message = store.send("run", "Use the visible button", "steer")
    assert await control.checkpoint() == message["content"]
    assert store.turn("run")["status"] == "running"


@pytest.mark.asyncio
async def test_fake_loop_plan_change_waits_for_continue(store):
    async def judge(message):
        return {"plan_change": True, "reply": "Proposed plan: test logout instead."}

    control = ChatControl(store, "run", judge, poll_seconds=0.001)
    await control.checkpoint()
    store.send("run", "Test logout instead", "change")

    async def human():
        while store.turn("run")["status"] != "paused":
            await asyncio.sleep(0)
        assert store.get("run")["messages"][-1]["content"] == "Proposed plan: test logout instead."
        store.send("run", "Continue", "continue", kind="continue")

    result, unused = await asyncio.gather(control.checkpoint(), human())
    assert result == "Test logout instead"
    assert store.turn("run")["status"] == "running"


@pytest.mark.asyncio
async def test_fake_loop_manual_pause_resumes_on_another_message(store):
    async def judge(message):
        return {"plan_change": False, "reply": "Continuing with your correction."}

    control = ChatControl(store, "run", judge, poll_seconds=0.001)
    await control.checkpoint()
    store.send("run", "Pause", "pause", kind="pause")

    async def human():
        while store.turn("run")["status"] != "paused":
            await asyncio.sleep(0)
        store.send("run", "Keep checking login", "correction")

    result, unused = await asyncio.gather(control.checkpoint(), human())
    assert result == "Keep checking login"


@pytest.mark.asyncio
async def test_cancel_interrupts_a_paused_checkpoint(store):
    control = ChatControl(store, "run", lambda message: None, poll_seconds=0.001)
    await control.checkpoint()
    store.send("run", "Pause", "pause", kind="pause")

    async def cancel():
        while store.turn("run")["status"] != "paused":
            await asyncio.sleep(0)
        store.transition("run", "cancelled")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(control.checkpoint(), cancel())


@pytest.mark.asyncio
async def test_bad_detector_output_pauses_instead_of_executing(store):
    async def judge(message):
        return {"plan_change": "false", "reply": "Untrusted"}

    control = ChatControl(store, "run", judge, poll_seconds=0.001)
    await control.checkpoint()
    store.send("run", "Correction", "correction")

    async def confirm():
        while store.turn("run")["status"] != "paused":
            await asyncio.sleep(0)
        assert "could not confirm" in store.get("run")["messages"][-1]["content"]
        store.send("run", "Continue", "confirm", kind="continue")

    result, unused = await asyncio.gather(control.checkpoint(), confirm())
    assert result == "Correction"
