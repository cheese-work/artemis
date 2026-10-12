import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore


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
