import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from artemis.data_engine.chat_sessions import ChatStore, TRANSITIONS
from artemis.runtime.chat_control import ChatControl, read_chat_guidance


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


@pytest.mark.asyncio
async def test_real_guidance_hook_uses_the_runs_operator_model(store, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CHAT_SESSION_ID", "run")
    monkeypatch.setenv("ARTEMIS_SESSION_ID", "run")
    monkeypatch.delenv("ARTEMIS_CHAT_TURN_STARTED", raising=False)
    monkeypatch.setattr("artemis.config.DB_PATH", store.path)
    calls = []

    class FakeModel:
        async def ainvoke(self, messages):
            return SimpleNamespace(
                content='{"plan_change": false, "reply": "I will steer the next step."}'
            )

    def model(context, name):
        calls.append((context, name))
        return FakeModel()

    monkeypatch.setattr("artemis.services.llm.get_llm", model)
    context = SimpleNamespace(data_engine=None)
    assert await read_chat_guidance(context, "Check login") == "Check login"
    store.send("run", "Use the visible button", "steer")
    assert await read_chat_guidance(context, "Check login") == "Use the visible button"
    assert calls == [(context, "operator")]


@pytest.mark.asyncio
async def test_typed_events_never_enter_follow_up_or_judge_history(store, monkeypatch):
    store.acknowledge(store.pending("run")[0]["sequence"], "Opening instruction received.")
    for kind in ("thinking", "step", "plan", "replan", "input_request"):
        store.record_event("run", kind, "internal-event-sentinel")
    store.finish("run", "completed")
    turn = store.send("run", "Check logout", "follow-up")
    monkeypatch.setenv("ARTEMIS_CHAT_SESSION_ID", "run")
    monkeypatch.setenv("ARTEMIS_SESSION_ID", turn["turn_id"])
    monkeypatch.delenv("ARTEMIS_CHAT_TURN_STARTED", raising=False)
    monkeypatch.setattr("artemis.config.DB_PATH", store.path)
    prompts = []

    class FakeModel:
        async def ainvoke(self, messages):
            prompts.extend(message.content for message in messages)
            return SimpleNamespace(content='{"plan_change": false, "reply": "I will steer."}')

    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *args, **kwargs: FakeModel())
    context = SimpleNamespace(data_engine=None)
    guidance = await read_chat_guidance(context, "Check logout")
    assert "Check login" in guidance
    assert "internal-event-sentinel" not in guidance
    store.send("run", "Use the visible button", "steer")
    await read_chat_guidance(context, "Check logout")
    assert prompts
    assert "internal-event-sentinel" not in "\n".join(prompts)


@pytest.mark.asyncio
async def test_guidance_boundary_emits_only_changed_plan_snapshots(store, tmp_path, monkeypatch):
    from artemis.utils.notes import get_note_file_path

    monkeypatch.setenv("ARTEMIS_CHAT_SESSION_ID", "run")
    control = ChatControl(store, "run", None, poll_seconds=0)
    context = SimpleNamespace(chat_control=control, data_engine=SimpleNamespace(base_dir=tmp_path))
    plan = get_note_file_path(tmp_path, "task_plan")
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("1. Open login", encoding="utf-8")
    await read_chat_guidance(context, "Check login")
    await read_chat_guidance(context, "Check login")
    plan.write_text("1. Open settings", encoding="utf-8")
    await read_chat_guidance(context, "Check login")
    events = [
        event for event in store.get("run")["messages"] if event["kind"] in {"plan", "replan"}
    ]
    assert [event["kind"] for event in events] == ["plan", "replan"]
    assert events[-1]["payload"]["previous"] == "1. Open login"


@pytest.mark.asyncio
async def test_unreadable_plan_does_not_fail_the_step_boundary(store, tmp_path, monkeypatch):
    from pathlib import Path
    from artemis.utils.notes import get_note_file_path

    monkeypatch.setenv("ARTEMIS_CHAT_SESSION_ID", "run")
    plan = get_note_file_path(tmp_path, "task_plan")
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("Plan", encoding="utf-8")
    context = SimpleNamespace(
        chat_control=ChatControl(store, "run", None), data_engine=SimpleNamespace(base_dir=tmp_path)
    )
    monkeypatch.setattr(
        Path, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Unavailable"))
    )
    assert await read_chat_guidance(context, "Check login") == "Check login"
    assert not any(event["kind"] == "plan" for event in store.get("run")["messages"])
