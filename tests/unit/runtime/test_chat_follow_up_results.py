import os
import sqlite3

import pytest

from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime import chat_session_worker
from artemis.runtime.chat_session_worker import follow_up_goal, run_chat_session

TABLE = (
    "| Mục | Trạng thái | Kết quả | Bằng chứng |\n|---|---|---|---|\n"
    "| Phiên 1 | PASS | 12 server | step 4 |\n| Phiên 2 | NOT TESTED | | |\n"
    "TOTAL: Planned 2 / PASS 1 / FAIL 0 / BLOCKED 0 / NOT TESTED 1\n"
)


class FakeLease:
    def acquire(self, **kwargs):
        pass

    def release(self):
        pass


@pytest.fixture
def traces(tmp_path, monkeypatch):
    path = tmp_path / "traces"
    path.mkdir()
    monkeypatch.setattr(chat_session_worker, "TRACES_PATH", path)
    return path


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, status TEXT, start_time REAL, end_time REAL)"
        )
        connection.execute("INSERT INTO sessions VALUES ('run', 'Check VPN', 'completed', 1, 2)")
    return ChatStore(path)


def _write(traces, turn_id, name, text):
    path = traces / turn_id / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _run_turn_two(store, traces, message="tiếp tục"):
    seen = []

    async def execute(turn):
        seen.append(turn)
        with store.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,1,2)",
                (turn["turn_id"], turn["goal"], "completed"),
            )

    store.send("run", message, "follow-up")
    await run_chat_session(
        store, "run", FakeLease(), execute, idle_seconds=0.01, poll_seconds=0.001
    )
    return seen[0]


@pytest.mark.asyncio
async def test_turn_two_gets_the_previous_results_copied_and_in_its_goal(store, traces):
    original = _write(traces, "run", "results.md", TABLE)
    os.utime(original, (1000, 1000))
    _write(traces, "run", "output.md", "Phiên 1 xong, phiên 2 chưa chạy.")

    turn = await _run_turn_two(store, traces)

    copy = traces / turn["turn_id"] / "notes" / "results.md"
    assert copy.read_text(encoding="utf-8") == TABLE
    assert copy.stat().st_mtime == 1000
    goal = follow_up_goal(turn)
    assert goal.startswith("tiếp tục")
    assert TABLE in goal
    assert "Phiên 1 xong, phiên 2 chưa chạy." in goal
    assert "NOT TESTED/BLOCKED" in goal
    assert turn["goal"] == "tiếp tục"


@pytest.mark.asyncio
async def test_copy_comes_from_latest_turn_that_has_results(store, traces):
    _write(traces, "run", "results.md", TABLE)
    failed = store.new_turn("run", "thử lại", "rejected")["turn_id"]
    store.fail_admission(failed, "Điện thoại chưa kết nối")

    turn = await _run_turn_two(store, traces)

    assert turn["number"] == 3
    assert not (traces / failed / "notes").exists()
    assert (traces / turn["turn_id"] / "notes" / "results.md").read_text(encoding="utf-8") == TABLE
    assert TABLE in follow_up_goal(turn)


@pytest.mark.asyncio
async def test_no_earlier_results_leaves_the_goal_alone(store, traces):
    turn = await _run_turn_two(store, traces)
    assert follow_up_goal(turn) == "tiếp tục"
    assert not (traces / turn["turn_id"] / "notes" / "results.md").exists()


@pytest.mark.asyncio
async def test_symlinked_results_are_not_copied(store, traces, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("not yours")
    notes = traces / "run" / "notes"
    notes.mkdir(parents=True)
    (notes / "results.md").symlink_to(outside)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"
    assert not (traces / turn["turn_id"] / "notes" / "results.md").exists()


@pytest.mark.asyncio
async def test_failed_copy_does_not_stop_the_turn(store, traces, monkeypatch):
    _write(traces, "run", "results.md", TABLE)

    def refuse(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(chat_session_worker.shutil, "copy2", refuse)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"


@pytest.fixture
def no_nofollow(monkeypatch):
    """Windows Python has no os.O_NOFOLLOW."""
    monkeypatch.delattr(os, "O_NOFOLLOW")


@pytest.mark.asyncio
async def test_follow_up_runs_and_ends_when_platform_has_no_nofollow(store, traces, no_nofollow):
    _write(traces, "run", "results.md", TABLE)

    turn = await _run_turn_two(store, traces)

    assert (traces / turn["turn_id"] / "notes" / "results.md").read_text(encoding="utf-8") == TABLE
    assert TABLE in follow_up_goal(turn)
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_symlinked_results_stay_rejected_without_nofollow(
    store, traces, tmp_path, no_nofollow
):
    outside = tmp_path / "secret.md"
    outside.write_text("not yours")
    notes = traces / "run" / "notes"
    notes.mkdir(parents=True)
    (notes / "results.md").symlink_to(outside)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"
    assert not (traces / turn["turn_id"] / "notes" / "results.md").exists()
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_unexpected_carry_error_still_runs_the_turn(store, traces, monkeypatch):
    def broken(*args, **kwargs):
        raise AttributeError("platform surprise")

    monkeypatch.setattr(chat_session_worker, "_read_note", broken)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"
    assert store.turn(turn["turn_id"])["status"] == "completed"
