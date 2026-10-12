import sqlite3

from artemis.data_engine.chat_sessions import ChatStore


def test_committed_terminal_outcome_recovers_unread_message_after_worker_exit(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run', 'Check login', 'running', 1, NULL)")
    store = ChatStore(path)
    message = store.send("run", "Use guest login", "pending")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sessions SET status='failed' WHERE session_id='run'")
    recovered = ChatStore(path)
    assert recovered.turn("run")["status"] == "failed"
    next_turn = recovered.next_turn("run")
    assert next_turn["turn_id"] != "run"
    assert recovered.pending(next_turn["turn_id"])[0]["sequence"] == message["sequence"]
