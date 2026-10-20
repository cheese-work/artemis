import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from apps.admin_console.core.state import state
from apps.admin_console.routers import chats
from apps.admin_console.services.task_queue_service import task_queue_service
from artemis.data_engine.chat_sessions import ChatStore


@pytest.fixture
def chat(library, monkeypatch):
    identity = library.seed("Check login", status="completed")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE run_meta SET requested_by='qa@example.com' WHERE session_id=?", (identity,)
        )
    monkeypatch.setattr(chats, "DB_PATH", library.db)
    monkeypatch.setattr(chats, "TRACES_PATH", library.traces)
    monkeypatch.setattr(state, "queue_items", [])
    ChatStore(library.db).save_launch_config(
        identity, {"device_serial": "emulator-5554", "host_id": "host-1"}
    )
    return identity


def _statuses(library, identity):
    return [turn["status"] for turn in ChatStore(library.db).get(identity)["turns"]]


def _queued_turn(library, identity, *, age=300.0):
    turn = ChatStore(library.db).new_turn(identity, "tiếp tục", f"stuck-{time.time_ns()}")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE chat_turns SET created_at=? WHERE turn_id=?",
            (time.time() - age, turn["turn_id"]),
        )
    return turn["turn_id"]


@pytest.mark.asyncio
async def test_rejected_enqueue_fails_turn_with_reason(qa, chat, library, monkeypatch):
    rejected = {"status": "rejected", "error": "Device emulator-5554 is not attached."}
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", AsyncMock(return_value=rejected))

    response = await qa.post(
        f"/api/chats/{chat}/messages", json={"content": "tiếp tục", "client_id": "follow-up"}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "session_device_unavailable"
    assert _statuses(library, chat) == ["completed", "failed"]
    replies = [
        m["content"] for m in ChatStore(library.db).get(chat)["messages"] if m["role"] == "agent"
    ]
    assert any("Device emulator-5554 is not attached." in reply for reply in replies)


@pytest.mark.asyncio
async def test_follow_up_behind_live_worker_is_not_failed(qa, chat, library, monkeypatch):
    with sqlite3.connect(library.db) as connection:
        connection.execute("UPDATE chat_sessions SET lease_pid=4242, lease_birth=1.0")
    monkeypatch.setattr(chats, "pid_is_alive", lambda pid, birth: True)
    enqueue = AsyncMock()
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)

    response = await qa.post(
        f"/api/chats/{chat}/messages", json={"content": "tiếp tục", "client_id": "follow-up"}
    )

    assert response.status_code == 200
    assert _statuses(library, chat) == ["completed", "queued"]
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_read_fails_stuck_queued_turn(qa, chat, library):
    _queued_turn(library, chat)

    response = await qa.get(f"/api/chats/{chat}")

    assert response.status_code == 200
    assert _statuses(library, chat) == ["completed", "failed"]
    assert response.json()["turns"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_chat_read_keeps_young_queued_turn(qa, chat, library):
    _queued_turn(library, chat, age=30.0)
    await qa.get(f"/api/chats/{chat}")
    assert _statuses(library, chat) == ["completed", "queued"]


@pytest.mark.asyncio
async def test_chat_read_keeps_turn_with_live_worker(qa, chat, library, monkeypatch):
    _queued_turn(library, chat)
    with sqlite3.connect(library.db) as connection:
        connection.execute("UPDATE chat_sessions SET lease_pid=4242, lease_birth=1.0")
    monkeypatch.setattr(chats, "pid_is_alive", lambda pid, birth: True)
    await qa.get(f"/api/chats/{chat}")
    assert _statuses(library, chat) == ["completed", "queued"]


@pytest.mark.asyncio
async def test_chat_read_keeps_turn_with_queue_item(qa, chat, library):
    turn_id = _queued_turn(library, chat)
    state.queue_items.append({"session_id": turn_id})
    await qa.get(f"/api/chats/{chat}")
    assert _statuses(library, chat) == ["completed", "queued"]


@pytest.mark.asyncio
async def test_chat_read_keeps_turn_with_sessions_row(qa, chat, library):
    turn_id = _queued_turn(library, chat)
    library.seed("tiếp tục", status="running", sid=turn_id)
    await qa.get(f"/api/chats/{chat}")
    assert _statuses(library, chat) == ["completed", "queued"]
