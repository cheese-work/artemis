import sqlite3
import uuid

import pytest

from artemis.data_engine.chat_sessions import ChatStore
from artemis.data_engine.storage import StorageManager


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('legacy', 'Check login', 'completed', 1, 2)"
        )
    return ChatStore(path)


def test_migration_is_additive_and_idempotent(store):
    store.migrate()
    store.migrate()
    session = store.get("legacy")
    assert session["session_id"] == "legacy"
    assert [(turn["turn_id"], turn["number"], turn["status"]) for turn in session["turns"]] == [
        ("legacy", 1, "completed")
    ]
    assert [(message["role"], message["content"]) for message in session["messages"]] == [
        ("human", "Check login")
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT * FROM sessions").fetchall() == [
            ("legacy", "Check login", "completed", 1, 2)
        ]


def test_follow_up_keeps_session_and_separate_turn_identity(store):
    turn = store.new_turn("legacy", "Check logout", "follow-up")
    assert turn["turn_id"] != "legacy"
    assert turn["number"] == 2
    session = store.get(turn["turn_id"])
    assert session["session_id"] == "legacy"
    assert [message["content"] for message in session["messages"]] == [
        "Check login",
        "Check logout",
    ]


def test_retry_does_not_create_another_turn_or_message(store):
    first = store.new_turn("legacy", "Check logout", "retry-key")
    second = store.new_turn("legacy", "Check logout", "retry-key")
    assert first == second
    assert len(store.get("legacy")["turns"]) == 2


def test_cannot_open_two_active_turns(store):
    store.new_turn("legacy", "First", "one")
    with pytest.raises(ValueError, match="active_turn"):
        store.new_turn("legacy", "Second", "two")


def test_unknown_session_is_not_created(store):
    with pytest.raises(KeyError):
        store.get("missing")


def test_migration_indexes_message_turn_lookup(store):
    store.migrate()
    with sqlite3.connect(store.path) as connection:
        columns = connection.execute("PRAGMA index_info(chat_message_turn)").fetchall()
        assert [column[2] for column in columns] == ["turn_id"]
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM sessions s WHERE NOT EXISTS "
            "(SELECT 1 FROM chat_messages m WHERE m.turn_id=s.session_id)"
        ).fetchall()
        assert any("chat_message_turn" in row[3] for row in plan)


@pytest.fixture
def chat_storage(tmp_path):
    storage = StorageManager(tmp_path / "sessions.db", tmp_path / "traces")
    root_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    with sqlite3.connect(storage.db_path) as connection:
        connection.executemany(
            "INSERT INTO sessions(session_id, initial_goal, status, start_time) VALUES (?,?,'completed',1)",
            [(root_id, "Private opening goal"), (other_id, "Unrelated goal")],
        )
    store = ChatStore(storage.db_path)
    turn = store.new_turn(root_id, "Private follow-up", "follow-up")
    with sqlite3.connect(storage.db_path) as connection:
        connection.execute(
            "UPDATE chat_sessions SET launch_config=? WHERE session_id=?",
            (
                '{"device_serial":"private-phone","host_id":"private-host","requester":"owner"}',
                root_id,
            ),
        )
    return storage, store, root_id, turn["turn_id"], other_id


def test_delete_root_removes_all_chat_turns_and_private_data(chat_storage):
    storage, store, root_id, turn_id, other_id = chat_storage
    storage.delete_session(uuid.UUID(root_id), delete_files=False, vacuum=False)
    recovered = ChatStore(storage.db_path)
    for identity in (root_id, turn_id):
        with pytest.raises(KeyError):
            recovered.get(identity)
    assert recovered.get(other_id)["messages"][0]["content"] == "Unrelated goal"
    with sqlite3.connect(store.path) as connection:
        for table in ("sessions", "chat_sessions", "chat_turns", "chat_messages"):
            assert connection.execute(f"SELECT session_id FROM {table}").fetchall() == [(other_id,)]
        assert connection.execute("SELECT launch_config FROM chat_sessions").fetchall() == [(None,)]


def test_delete_turn_keeps_root_and_unrelated_chat(chat_storage):
    storage, store, root_id, turn_id, other_id = chat_storage
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO sessions(session_id, initial_goal, status, start_time) VALUES (?,?,'completed',2)",
            (turn_id, "Private follow-up"),
        )
    storage.delete_session(uuid.UUID(turn_id), delete_files=False, vacuum=False)
    recovered = ChatStore(storage.db_path)
    with pytest.raises(KeyError):
        recovered.get(turn_id)
    session = recovered.get(root_id)
    assert [turn["turn_id"] for turn in session["turns"]] == [root_id]
    assert [message["content"] for message in session["messages"]] == ["Private opening goal"]
    assert recovered.get(other_id)["session_id"] == other_id
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute(
                "SELECT launch_config FROM chat_sessions WHERE session_id=?", (root_id,)
            ).fetchone()[0]
            is not None
        )


def test_clear_all_data_removes_chat_payload_and_launch_config(chat_storage):
    storage, store, root_id, turn_id, other_id = chat_storage
    storage.clear_all_data()
    recovered = ChatStore(storage.db_path)
    for identity in (root_id, turn_id, other_id):
        with pytest.raises(KeyError):
            recovered.get(identity)
    with sqlite3.connect(store.path) as connection:
        for table in ("sessions", "chat_sessions", "chat_turns", "chat_messages"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_get_missing_root_raises_not_found_instead_of_type_error(store):
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM chat_sessions WHERE session_id='legacy'")
    with pytest.raises(KeyError, match="legacy"):
        store.get("legacy")
