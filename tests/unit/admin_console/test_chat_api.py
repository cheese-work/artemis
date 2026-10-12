import sqlite3

import pytest

from apps.admin_console.routers import chats
from artemis.data_engine.chat_sessions import ChatStore


@pytest.fixture
def chat(library, monkeypatch):
    identity = library.seed("Check login", status="running")
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE run_meta SET requested_by='qa@example.com' WHERE session_id=?", (identity,)
        )
    monkeypatch.setattr(chats, "DB_PATH", library.db)

    async def no_worker(*args):
        pass

    monkeypatch.setattr(chats, "_ensure_worker", no_worker)
    return identity


@pytest.mark.asyncio
async def test_owner_reads_and_sends_in_order(qa, chat):
    first = await qa.post(
        f"/api/chats/{chat}/messages", json={"content": "Check logout", "client_id": "one"}
    )
    assert first.status_code == 200
    second = await qa.post(
        f"/api/chats/{chat}/messages", json={"content": "Check settings", "client_id": "two"}
    )
    assert second.status_code == 200
    response = await qa.get(f"/api/chats/{chat}")
    assert response.status_code == 200
    assert [item["content"] for item in response.json()["messages"]] == [
        "Check login",
        "Check logout",
        "Check settings",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [("messages", {"content": "Secret", "client_id": "one"}), ("pause", {}), ("continue", {})],
)
async def test_nonowner_cannot_control_session(qa2, chat, path, payload, library):
    before = ChatStore(library.db).get(chat)
    response = await qa2.post(f"/api/chats/{chat}/{path}", json=payload)
    assert response.status_code == 403
    assert ChatStore(library.db).get(chat) == before


@pytest.mark.asyncio
async def test_anonymous_cannot_read_chat(anonymous, chat):
    assert (await anonymous.get(f"/api/chats/{chat}")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_owner_pause_and_continue_are_ordered_commands(qa, chat):
    assert (
        await qa.post(f"/api/chats/{chat}/pause", json={"client_id": "pause"})
    ).status_code == 200
    assert (
        await qa.post(f"/api/chats/{chat}/continue", json={"client_id": "continue"})
    ).status_code == 200
    messages = (await qa.get(f"/api/chats/{chat}")).json()["messages"]
    assert [message["kind"] for message in messages] == ["message", "pause", "continue"]


@pytest.mark.asyncio
async def test_follow_up_shares_session_not_evidence(qa, chat, library):
    store = ChatStore(library.db)
    store.transition(chat, "completed")
    response = await qa.post(
        f"/api/chats/{chat}/messages", json={"content": "Another task", "client_id": "follow-up"}
    )
    assert response.status_code == 200
    turn_id = response.json()["turn_id"]
    assert turn_id != chat
    thread = (await qa.get(f"/api/chats/{turn_id}")).json()
    assert thread["session_id"] == chat
    assert len(thread["turns"]) == 2
