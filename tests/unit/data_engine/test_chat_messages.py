import concurrent.futures
import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES ('run', 'Open settings', 'queued', 1, NULL)"
        )
    return ChatStore(path)


def test_queued_bubbles_are_fifo_and_persist_until_acknowledged(store):
    first = store.send("run", "Check theme", "one")
    second = store.send("run", "Check language", "two")
    pending = ChatStore(store.path).pending("run")
    assert [message["content"] for message in pending] == [
        "Open settings",
        "Check theme",
        "Check language",
    ]
    assert first["sequence"] < second["sequence"]
    store.acknowledge(first["sequence"], "I will check theme.")
    assert [message["content"] for message in store.pending("run")] == [
        "Open settings",
        "Check language",
    ]
    assert store.get("run")["messages"][-1]["reply_to"] == first["sequence"]


def test_acknowledgement_and_reply_are_atomic_and_idempotent(store):
    message = store.send("run", "Check theme", "one")
    store.acknowledge(message["sequence"], "Understood")
    store.acknowledge(message["sequence"], "Duplicate")
    replies = [
        item for item in store.get("run")["messages"] if item["reply_to"] == message["sequence"]
    ]
    assert [item["content"] for item in replies] == ["Understood"]


def test_concurrent_retries_create_one_message(store):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(
            executor.map(lambda unused: store.send("run", "Check theme", "same-key"), range(8))
        )
    assert len({item["sequence"] for item in results}) == 1
    assert len(store.pending("run")) == 2


def test_client_id_cannot_be_reused_for_different_content(store):
    store.send("run", "First", "one")
    with pytest.raises(ValueError, match="client_id_conflict"):
        store.send("run", "Different", "one")


def test_messages_cannot_cross_session_boundaries(store):
    with pytest.raises(KeyError):
        store.send("unknown", "Check theme", "one")


@pytest.mark.parametrize(
    "content,client_id",
    [("", "one"), ("x" * 32769, "one"), ("hello", ""), ("hello", "x" * 129)],
    ids=["empty-content", "long-content", "empty-id", "long-id"],
)
def test_message_limits(store, content, client_id):
    with pytest.raises(ValueError, match="invalid_message"):
        store.send("run", content, client_id)
