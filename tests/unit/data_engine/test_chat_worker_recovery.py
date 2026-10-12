import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore


def test_completed_outcome_recovers_unread_message_after_worker_exit(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run', 'Check login', 'running', 1, NULL)")
    store = ChatStore(path)
    message = store.send("run", "Use guest login", "pending")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET status='completed' WHERE session_id='run'")
    recovered = ChatStore(path)
    assert recovered.turn("run")["status"] == "completed"
    next_turn = recovered.next_turn("run")
    assert next_turn["turn_id"] != "run"
    assert recovered.pending(next_turn["turn_id"])[0]["sequence"] == message["sequence"]


@pytest.mark.parametrize("status", ["cancelled", "failed", "interrupted"])
@pytest.mark.parametrize("phase", ["queued", "running", "paused"])
@pytest.mark.parametrize("recover_from_catalog", [False, True])
def test_unsuccessful_turn_acknowledges_pending_messages_without_replay(
    tmp_path, status, phase, recover_from_catalog
):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run','Opening goal','completed',1,2)")
    store = ChatStore(path)
    first = store.send("run", "Cancelled goal", "first")
    turn_id = first["turn_id"]
    second = store.send("run", "Cancelled steering", "second")
    control = store.send("run", "Pause", "pause", kind="pause")
    if phase != "queued":
        store.transition(turn_id, "running")
    if phase == "paused":
        store.transition(turn_id, "paused")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,2,3)", (turn_id, "Cancelled goal", status)
        )
    recovered = ChatStore(path) if recover_from_catalog else store
    if not recover_from_catalog:
        recovered.finish(turn_id, status)
    assert recovered.turn(turn_id)["status"] == status
    assert recovered.next_turn("run") is None
    assert recovered.pending(turn_id) == []
    session = recovered.get("run")
    assert len(session["turns"]) == 2
    sequences = {first["sequence"], second["sequence"], control["sequence"]}
    settled = [message for message in session["messages"] if message["sequence"] in sequences]
    assert all(message["acknowledged_at"] is not None for message in settled)
    replies = [message for message in session["messages"] if message["reply_to"] in sequences]
    assert {message["reply_to"] for message in replies} == sequences
    assert all(message["role"] == "agent" and status in message["content"] for message in replies)
    recovered.finish(turn_id, status)
    assert recovered.get("run")["messages"] == session["messages"]
    fresh = recovered.send("run", "New explicit goal", "fresh")
    assert fresh["turn_id"] != turn_id
    assert [message["content"] for message in recovered.pending(fresh["turn_id"])] == [
        "New explicit goal"
    ]


@pytest.mark.parametrize("status", ["queued", "running", "paused", "unknown"])
def test_finish_rejects_nonterminal_status_without_acknowledging_messages(tmp_path, status):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run','Opening goal','running',1,NULL)")
    store = ChatStore(path)
    message = store.send("run", "Pending instruction", "pending")
    with pytest.raises(ValueError):
        store.finish("run", status)
    assert store.turn("run")["status"] == "running"
    assert store.pending("run") == [message]
