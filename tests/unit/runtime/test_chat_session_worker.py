import asyncio
import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime.chat_session_worker import run_chat_session


class FakeLease:
    def __init__(self):
        self.held = False
        self.acquisitions = 0

    def acquire(self, **kwargs):
        self.held = True
        self.acquisitions += 1

    def release(self):
        self.held = False


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run', 'Check login', 'queued', 1, NULL)")
    return ChatStore(path)


@pytest.mark.asyncio
async def test_fake_loop_follow_up_keeps_one_lease_and_full_history(store):
    lease = FakeLease()
    seen = []

    async def execute(turn):
        assert lease.held
        seen.append(turn)
        with store.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,1,2)",
                (turn["turn_id"], turn["goal"], "completed"),
            )

    async def human():
        while store.turn("run")["status"] != "completed":
            await asyncio.sleep(0)
        assert lease.held
        store.send("run", "Check logout", "follow-up")

    await asyncio.gather(
        run_chat_session(store, "run", lease, execute, idle_seconds=0.03, poll_seconds=0.001),
        human(),
    )
    assert [turn["goal"] for turn in seen] == ["Check login", "Check logout"]
    assert seen[0]["turn_id"] != seen[1]["turn_id"]
    assert "Check login" in seen[1]["history"]
    assert lease.acquisitions == 1
    assert not lease.held
    assert store.get("run")["lease_pid"] is None


@pytest.mark.asyncio
async def test_follow_up_after_idle_expiry_reacquires(store):
    lease = FakeLease()

    async def execute(turn):
        with store.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,1,2)",
                (turn["turn_id"], turn["goal"], "completed"),
            )

    await run_chat_session(store, "run", lease, execute, idle_seconds=0, poll_seconds=0)
    store.send("run", "Check logout", "follow-up")
    await run_chat_session(store, "run", lease, execute, idle_seconds=0, poll_seconds=0)
    assert lease.acquisitions == 2
    assert [turn["status"] for turn in store.get("run")["turns"]] == ["completed", "completed"]


@pytest.mark.asyncio
async def test_finish_race_moves_unread_bubble_to_next_turn(store):
    lease = FakeLease()
    seen = []

    async def execute(turn):
        seen.append(turn["goal"])
        if len(seen) == 1:
            store.send("run", "Arrived at final step", "late")
        with store.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,1,2)",
                (turn["turn_id"], turn["goal"], "completed"),
            )

    await run_chat_session(store, "run", lease, execute, idle_seconds=0, poll_seconds=0)
    assert seen == ["Check login", "Arrived at final step"]


@pytest.mark.parametrize("status", ["cancelled", "failed", "interrupted"])
@pytest.mark.asyncio
async def test_unsuccessful_queued_follow_up_is_not_executed_after_a_fresh_message(store, status):
    store.acknowledge(store.pending("run")[0]["sequence"], "Opening instruction handled")
    store.finish("run", "completed")
    abandoned = store.send("run", "Do not run this goal", "abandoned")
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO sessions VALUES (?,?,?,1,2)",
            (abandoned["turn_id"], abandoned["content"], status),
        )
    recovered = ChatStore(store.path)
    fresh = recovered.send("run", "Run only the new goal", "fresh")
    lease = FakeLease()
    seen = []

    async def execute(turn):
        seen.append((turn["turn_id"], turn["goal"]))
        with recovered.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,1,2)",
                (turn["turn_id"], turn["goal"], "completed"),
            )

    await run_chat_session(recovered, "run", lease, execute, idle_seconds=0, poll_seconds=0)
    assert seen == [(fresh["turn_id"], "Run only the new goal")]
    assert not lease.held


@pytest.mark.asyncio
async def test_failed_turn_can_continue_without_leaking_lease(store):
    lease = FakeLease()

    async def execute(turn):
        raise RuntimeError("Phone disconnected")

    await run_chat_session(store, "run", lease, execute, idle_seconds=0, poll_seconds=0)
    assert store.turn("run")["status"] == "failed"
    assert not lease.held


@pytest.mark.asyncio
async def test_acquisition_failure_is_clear_and_releases_session_claim(store):
    class UnavailableLease(FakeLease):
        def acquire(self, **kwargs):
            raise RuntimeError("session_device_unavailable")

    with pytest.raises(RuntimeError, match="session_device_unavailable"):
        await run_chat_session(store, "run", UnavailableLease(), lambda turn: None, idle_seconds=0)
    assert store.get("run")["lease_pid"] is None
