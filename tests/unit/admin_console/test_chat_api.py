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


@pytest.mark.asyncio
async def test_visible_qa_can_rename_without_gaining_device_control(qa2, qa, chat, library):
    response = await qa2.patch(f"/api/chats/{chat}/title", json={"title": "  Login checks  "})
    assert response.status_code == 200
    assert (await qa.get(f"/api/chats/{chat}")).json()["title"] == "Login checks"
    assert ChatStore(library.db).get(chat)["title"] == "Login checks"
    denied = await qa2.post(f"/api/chats/{chat}/pause", json={"client_id": "forbidden"})
    assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", "   ", "a" * 121])
async def test_invalid_rename_does_not_write(qa, chat, library, title):
    before = ChatStore(library.db).get(chat)
    assert (await qa.patch(f"/api/chats/{chat}/title", json={"title": title})).status_code == 422
    assert ChatStore(library.db).get(chat) == before


@pytest.mark.asyncio
async def test_rename_follows_turn_identity_and_survives_migration(qa, chat, library):
    store = ChatStore(library.db)
    store.transition(chat, "completed")
    turn = store.send(chat, "Next task", "next")
    response = await qa.patch(f"/api/chats/{turn['turn_id']}/title", json={"title": "a" * 120})
    assert response.status_code == 200
    store.migrate()
    assert store.get(chat)["title"] == "a" * 120
    catalog = (await qa.get(f"/api/runs/{chat}")).json()
    assert catalog["chat_title"] == "a" * 120
    assert catalog["chat_session_id"] == chat


@pytest.mark.asyncio
async def test_catalog_redacts_shared_title_and_first_prompt(qa, qa2, chat, library):
    secret = "catalog-test-secret"
    with sqlite3.connect(library.db) as connection:
        connection.execute(
            "UPDATE sessions SET initial_goal=? WHERE session_id=?",
            (f"Login with password={secret}", chat),
        )
    response = await qa.patch(
        f"/api/chats/{chat}/title", json={"title": f"Login password={secret}"}
    )
    assert response.status_code == 200
    owner = (await qa.get(f"/api/runs/{chat}")).json()
    assert secret in owner["chat_title"]
    assert secret in owner["chat_prompt"]
    shared = await qa2.get(f"/api/runs/{chat}")
    team = await qa.get("/api/runs", params={"scope": "everyone"})
    assert shared.status_code == team.status_code == 200
    for row in [shared.json(), *team.json()["runs"]]:
        assert row["read_only"]
        assert secret not in row["chat_title"]
        assert secret not in row["chat_prompt"]
        assert "[REDACTED]" in row["chat_title"]


@pytest.mark.asyncio
async def test_rename_rejects_anonymous_and_missing_session(anonymous, qa, chat):
    assert (
        await anonymous.patch(f"/api/chats/{chat}/title", json={"title": "No"})
    ).status_code in (401, 403)
    assert (await qa.patch("/api/chats/missing/title", json={"title": "No"})).status_code == 404


@pytest.mark.asyncio
async def test_rename_retains_completed_timeline_and_verdict(qa, chat, library, monkeypatch):
    ChatStore(library.db).transition(chat, "completed")
    monkeypatch.setattr(chats.step_repo, "get_session_steps", lambda identity: [])
    before = (await qa.get(f"/api/chats/{chat}")).json()
    renamed = (await qa.patch(f"/api/chats/{chat}/title", json={"title": "Renamed"})).json()
    assert renamed["timeline"] == before["timeline"]
    assert renamed["verdict"] == before["verdict"]


@pytest.mark.asyncio
async def test_timeline_hydrates_legacy_and_new_steps_and_redacts_shared_data(
    qa, qa2, chat, library, monkeypatch
):
    store = ChatStore(library.db)
    store.record_event(chat, "step", "Tap sign in", payload={"step_id": "new-step"})
    steps = [
        {
            "step_id": identity,
            "step_number": number,
            "timestamp": number,
            "summary": "Tap sign in",
            "action_taken": {"password": "timeline-secret"},
        }
        for number, identity in enumerate(["legacy-step", "new-step"], start=1)
    ]
    monkeypatch.setattr(chats.step_repo, "get_session_steps", lambda identity: steps)
    owner = (await qa.get(f"/api/chats/{chat}")).json()
    hydrated = [event for event in owner["timeline"] if event["kind"] == "step"]
    assert len(hydrated) == 2
    assert [event["payload"]["step_id"] for event in hydrated] == ["legacy-step", "new-step"]
    assert all(
        event["payload"]["action_taken"]["password"] == "timeline-secret" for event in hydrated
    )
    shared = await qa2.get(f"/api/chats/{chat}")
    assert shared.status_code == 200
    assert "timeline-secret" not in shared.text
    assert "[REDACTED]" in shared.text
    assert store.get(chat)["messages"][-1]["payload"] == {"step_id": "new-step"}


@pytest.mark.asyncio
async def test_unreadable_report_does_not_break_completed_chat(qa, chat, library, monkeypatch):
    from pathlib import Path

    store = ChatStore(library.db)
    store.transition(chat, "completed")
    report = library.traces / chat / "notes" / "output.md"
    report.parent.mkdir(parents=True)
    report.write_text("Report", encoding="utf-8")
    monkeypatch.setattr(chats, "TRACES_PATH", library.traces)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == report:
            raise OSError("Report not available")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    response = await qa.get(f"/api/chats/{chat}")
    assert response.status_code == 200
    assert response.json()["verdict"]["report"] is None
