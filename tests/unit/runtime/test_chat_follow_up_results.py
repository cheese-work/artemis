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

    monkeypatch.setattr(chat_session_worker.shutil, "copyfileobj", refuse)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"


@pytest.fixture
def no_nofollow(monkeypatch):
    """Linux simulation of Windows: no os.O_NOFOLLOW, and the handle-to-path lookup via /proc.

    Native Windows (GetFinalPathNameByHandleW) is not exercised here.
    """
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("simulation needs /proc/self/fd")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(
        chat_session_worker,
        "_handle_path",
        lambda fd: os.readlink(f"/proc/self/fd/{fd}"),
        raising=False,
    )


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

    monkeypatch.setattr(chat_session_worker, "_open_note", broken)

    turn = await _run_turn_two(store, traces)

    assert follow_up_goal(turn) == "tiếp tục"
    assert store.turn(turn["turn_id"])["status"] == "completed"


SECRET = "SECRET outside the trace tree"


@pytest.fixture(params=["pinned", "flag_absent"])
def mode(request, monkeypatch):
    """Both read strategies: descriptor-pinned walk, and the platform without O_NOFOLLOW."""
    if request.param == "flag_absent":
        request.getfixturevalue("no_nofollow")
    return request.param


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "results.md").write_text(SECRET)
    return folder


def _swap_once(monkeypatch, names, swap):
    """Run swap() just before the first os.open of one of names: a deterministic race."""
    real_open = os.open
    fired = []

    def racing_open(path, *args, **kwargs):
        if not fired and os.path.basename(os.fspath(path)) in names:
            fired.append(path)
            swap()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    return fired


def _assert_nothing_leaked(turn, traces):
    copy = traces / turn["turn_id"] / "notes" / "results.md"
    assert not copy.exists() or SECRET not in copy.read_text(encoding="utf-8")
    assert SECRET not in follow_up_goal(turn)


@pytest.mark.asyncio
async def test_leaf_swapped_for_a_link_before_open_is_not_followed(
    store, traces, outside, mode, monkeypatch
):
    leaf = _write(traces, "run", "results.md", TABLE)

    def swap():
        leaf.unlink()
        leaf.symlink_to(outside / "results.md")

    fired = _swap_once(monkeypatch, {"results.md"}, swap)

    turn = await _run_turn_two(store, traces)

    assert fired
    _assert_nothing_leaked(turn, traces)
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_ancestor_swapped_for_a_link_is_not_followed(
    store, traces, outside, mode, monkeypatch
):
    _write(traces, "run", "results.md", TABLE)
    notes = traces / "run" / "notes"

    def swap():
        notes.rename(traces / "run" / "notes-real")
        notes.symlink_to(outside, target_is_directory=True)

    fired = _swap_once(monkeypatch, {"notes", "results.md"}, swap)

    turn = await _run_turn_two(store, traces)

    assert fired
    _assert_nothing_leaked(turn, traces)
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_source_swapped_while_copying_does_not_change_the_copy(
    store, traces, outside, mode, monkeypatch
):
    leaf = _write(traces, "run", "results.md", TABLE)
    real_copy = chat_session_worker.shutil.copyfileobj
    swapped = []

    def swap_then_copy(*args, **kwargs):
        leaf.unlink()
        leaf.symlink_to(outside / "results.md")
        swapped.append(True)
        return real_copy(*args, **kwargs)

    monkeypatch.setattr(chat_session_worker.shutil, "copyfileobj", swap_then_copy)

    turn = await _run_turn_two(store, traces)

    assert swapped
    copy = traces / turn["turn_id"] / "notes" / "results.md"
    assert copy.read_text(encoding="utf-8") == TABLE
    _assert_nothing_leaked(turn, traces)


@pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="POSIX descriptor-relative opens")
def test_posix_reads_walk_the_tree_by_descriptor():
    assert chat_session_worker._OPEN_PINS_PARENTS


def _turn_dir(traces):
    """The new turn's trace folder: the only one that is not the first turn's."""
    return next(path for path in traces.iterdir() if path.name != "run")


def _swap_before_write_open(monkeypatch, swap):
    """Run swap() just before the first write-mode os.open: the destination file's open."""
    real_open = os.open
    fired = []

    def racing_open(path, flags, *args, **kwargs):
        if not fired and flags & os.O_WRONLY:
            fired.append(path)
            swap()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    return fired


@pytest.mark.asyncio
@pytest.mark.parametrize("swapped", ["notes", "turn"])
async def test_destination_ancestor_swapped_for_a_link_does_not_redirect_the_write(
    store, traces, outside, tmp_path, mode, monkeypatch, swapped
):
    _write(traces, "run", "results.md", TABLE)
    (outside / "notes").mkdir()
    (outside / "notes" / "results.md").write_text(SECRET)

    def swap():
        victim = _turn_dir(traces) / "notes" if swapped == "notes" else _turn_dir(traces)
        victim.rename(victim.with_name(victim.name + "-real"))
        victim.symlink_to(
            outside / "notes" if swapped == "notes" else outside, target_is_directory=True
        )

    fired = _swap_before_write_open(monkeypatch, swap)

    turn = await _run_turn_two(store, traces)

    assert fired
    assert (outside / "results.md").read_text() == SECRET
    assert (outside / "notes" / "results.md").read_text() == SECRET
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_destination_leaf_planted_as_a_link_is_not_followed(
    store, traces, outside, mode, monkeypatch
):
    _write(traces, "run", "results.md", TABLE)
    real_mkdir = os.mkdir

    def mkdir_then_plant(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        if os.path.basename(os.fspath(path)) == "notes":
            (_turn_dir(traces) / "notes" / "results.md").symlink_to(outside / "results.md")

    monkeypatch.setattr(os, "mkdir", mkdir_then_plant)

    turn = await _run_turn_two(store, traces)

    assert (outside / "results.md").read_text() == SECRET
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_timestamp_update_does_not_follow_a_swapped_destination_leaf(
    store, traces, outside, mode, monkeypatch
):
    original = _write(traces, "run", "results.md", TABLE)
    os.utime(original, (1000, 1000))
    os.utime(outside / "results.md", (5000, 5000))
    real_utime = os.utime
    fired = []

    def swap_then_utime(target, *args, **kwargs):
        if not fired:
            fired.append(target)
            leaf = _turn_dir(traces) / "notes" / "results.md"
            leaf.unlink()
            leaf.symlink_to(outside / "results.md")
        return real_utime(target, *args, **kwargs)

    monkeypatch.setattr(os, "utime", swap_then_utime)

    turn = await _run_turn_two(store, traces)

    assert fired
    assert (outside / "results.md").stat().st_mtime == 5000
    assert store.turn(turn["turn_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_source_swapped_to_a_link_and_back_between_checks_is_not_read(
    store, traces, outside, mode, monkeypatch
):
    """The double swap a path-only check on Windows cannot see: link at open, restored before the final check."""
    _write(traces, "run", "results.md", TABLE)
    notes = traces / "run" / "notes"
    swapped = []

    def to_link():
        notes.rename(traces / "run" / "notes-real")
        notes.symlink_to(outside, target_is_directory=True)
        swapped.append(True)

    def back():
        notes.unlink()
        (traces / "run" / "notes-real").rename(notes)
        swapped.clear()

    fired = _swap_once(monkeypatch, {"notes", "results.md"}, to_link)
    real_stat, real_lstat = os.stat, os.lstat

    def restoring(real):
        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            if swapped:
                back()
            return result

        return wrapper

    monkeypatch.setattr(os, "stat", restoring(real_stat))
    monkeypatch.setattr(os, "lstat", restoring(real_lstat))

    turn = await _run_turn_two(store, traces)

    assert fired
    _assert_nothing_leaked(turn, traces)
    assert store.turn(turn["turn_id"])["status"] == "completed"
