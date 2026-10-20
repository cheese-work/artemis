import asyncio
import json
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
from pathlib import Path

from artemis.config import TRACES_PATH
from artemis.data_engine.chat_sessions import TERMINAL
from artemis.runtime.cancel_requests import current_process_created_at, is_cancel_requested

logger = logging.getLogger(__name__)


NOTE_LIMIT = 64 * 1024


# Descriptor-relative opens need O_DIRECTORY and dir_fd support (POSIX); checked once at import.
_OPEN_PINS_PARENTS = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd


def _open_pinned(parts):
    """Walk from TRACES_PATH one component at a time; no step may follow a link."""
    flags = os.O_RDONLY | os.O_NOFOLLOW
    dir_fd = os.open(TRACES_PATH, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        return os.open(parts[-1], flags, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _open_checked(parts):
    """Without O_NOFOLLOW (Windows): reject links, open, then confirm the path still names that file."""
    root = Path(os.path.realpath(TRACES_PATH))
    path = root.joinpath(*parts)
    if any(root.joinpath(*parts[:depth]).is_symlink() for depth in range(1, len(parts) + 1)):
        raise OSError("link in note path")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    same = False
    try:
        same = (
            os.path.samestat(os.fstat(fd), os.stat(path, follow_symlinks=False))
            and Path(os.path.realpath(path)) == path
        )
    finally:
        if not same:
            os.close(fd)
    if not same:
        raise OSError("note path changed while opening")
    return fd


def _open_note(turn_id, name):
    """A turn's note opened read-only and binary, or None; links in the path are refused.

    Every later read and copy uses the returned handle, never the path again.
    """
    parts = (turn_id, "notes", name)
    try:
        pinned = _OPEN_PINS_PARENTS and hasattr(os, "O_NOFOLLOW")
        file = os.fdopen(_open_pinned(parts) if pinned else _open_checked(parts), "rb")
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
        file.close()
        return None
    return file


def _read_note(turn_id, name):
    """The first 64 KB of a turn's note as text, or None."""
    file = _open_note(turn_id, name)
    if file is None:
        return None
    with file:
        return file.read(NOTE_LIMIT).decode("utf-8", errors="ignore")


def _copy_note(file, path):
    """Write the whole open note to a new file, keeping its mtime; the destination is not a link."""
    info = os.fstat(file.fileno())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags | getattr(os, "O_BINARY", 0), 0o644), "wb") as out:
        shutil.copyfileobj(file, out)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


def carry_results(store, identity, turn):
    """Copy the latest earlier results.md into the new turn; return the context its goal needs."""
    earlier = [t for t in store.get(identity)["turns"] if t["number"] < turn["number"]]
    for previous in reversed(earlier):
        file = _open_note(previous["turn_id"], "results.md")
        if file:
            break
    else:
        return None
    with file:
        results = file.read(NOTE_LIMIT).decode("utf-8", errors="ignore")
        file.seek(0)
        target = TRACES_PATH / turn["turn_id"] / "notes"
        try:
            target.mkdir(parents=True, exist_ok=True)
            _copy_note(file, target / "results.md")
        except OSError:
            logger.warning(
                "Could not carry results.md into turn %s", turn["turn_id"], exc_info=True
            )
            return None
    return {"results": results, "report": _read_note(earlier[-1]["turn_id"], "output.md")}


def follow_up_goal(turn):
    """The user's message, plus the previous results so the agent runs only what is left."""
    context = turn.get("resume_context")
    if not context:
        return turn["goal"]
    parts = [
        turn["goal"],
        "Kết quả lượt trước (results.md, đã có sẵn trong notes):",
        context["results"],
    ]
    if context["report"]:
        parts += ["Báo cáo cuối của lượt trước:", context["report"]]
    parts.append(
        "Đọc results.md, giữ nguyên các mục đã xong và chỉ chạy các mục NOT TESTED/BLOCKED, "
        "trừ khi user nói khác. Không tạo lại bảng từ đầu."
    )
    return "\n\n".join(parts)


async def run_chat_session(store, identity, lease, execute, *, idle_seconds=300, poll_seconds=0.25):
    pid = os.getpid()
    store.claim_worker(identity, pid, current_process_created_at())
    cancel_event = threading.Event()
    acquired = False
    deadline = None
    turn_id = None
    pending_tips = []
    try:
        acquisition = asyncio.create_task(
            asyncio.to_thread(lease.acquire, cancel_event=cancel_event)
        )
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.shield(acquisition)
            finally:
                lease.release()
            raise
        acquired = True
        while not is_cancel_requested(session_id=identity, pid=pid):
            turn = store.next_turn(identity)
            if turn:
                turn_id = turn["turn_id"]
                store.transition(turn_id, "running")
                store.idle(identity, pid, None)
                opening = store.pending(turn_id)
                humans = [message["content"] for message in opening if message["role"] == "human"]
                turn["goal"] = "\n\n".join(humans)
                try:
                    turn["resume_context"] = carry_results(store, identity, turn)
                except Exception:
                    # Carrying results is best effort: the turn must still run and end.
                    logger.exception("Could not carry results into turn %s", turn_id)
                    turn["resume_context"] = None
                turn["history"] = json.dumps(store.model_history(identity), ensure_ascii=False)
                for message in opening:
                    if message["role"] == "human":
                        store.acknowledge(message["sequence"], "Opening instruction received.")
                outcome = "completed"
                try:
                    result = (await asyncio.gather(execute(turn), return_exceptions=True))[0]
                    if isinstance(result, asyncio.CancelledError):
                        raise result
                except asyncio.CancelledError:
                    store.finish(turn_id, "cancelled")
                    raise
                if isinstance(result, Exception):
                    logger.error(
                        "Chat turn %s failed",
                        turn_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    outcome = "failed"
                with sqlite3.connect(store.path) as connection:
                    row = connection.execute(
                        "SELECT status FROM sessions WHERE session_id=?", (turn_id,)
                    ).fetchone()
                    if row and row[0] in TERMINAL:
                        outcome = row[0]
                store.finish(turn_id, outcome)
                if outcome == "completed" and os.getenv("ARTEMIS_TASK_WORKER") == "1":
                    from apps.admin_console.services.automatic_prompt_tips import ensure_tip

                    pending_tips.append(asyncio.create_task(ensure_tip(turn_id)))
                deadline = time.time() + max(0, idle_seconds)
                store.idle(identity, pid, deadline)
                turn_id = None
            else:
                deadline = deadline if deadline is not None else time.time() + max(0, idle_seconds)
                if time.time() >= deadline and store.release_worker(
                    identity, pid, only_if_idle=True
                ):
                    return
                await asyncio.sleep(poll_seconds)
    finally:
        if not acquired:
            pending_turn = store.next_turn(identity)
            if pending_turn:
                store.fail_admission(
                    pending_turn["turn_id"],
                    "The session phone could not be re-acquired. Reconnect the original phone, then send another message.",
                )
        try:
            if acquired:
                await asyncio.to_thread(lease.release)
        finally:
            try:
                store.release_worker(identity, pid)
            finally:
                results = await asyncio.gather(*pending_tips, return_exceptions=True)
                if any(isinstance(result, BaseException) for result in results):
                    logger.warning("Automatic prompt tip unavailable for a completed chat turn.")
