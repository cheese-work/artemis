import asyncio
import io
import json
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path

from artemis.config import TRACES_PATH
from artemis.data_engine.chat_sessions import TERMINAL
from artemis.runtime.cancel_requests import current_process_created_at, is_cancel_requested

logger = logging.getLogger(__name__)


NOTE_LIMIT = 64 * 1024


# Checked once at import: tests and wrappers may replace these functions later.
_DIR_FD_OPS = all(call in os.supports_dir_fd for call in (os.open, os.mkdir, os.rename, os.unlink))


def _carry_supported():
    """Carrying results needs no-follow, descriptor-relative opens: POSIX only (not Windows).

    Elsewhere a turn runs without the copied results; nothing under TRACES_PATH is touched.
    """
    return hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and _DIR_FD_OPS


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


def _open_note(turn_id, name):
    """A turn's note opened read-only and binary, or None; links in the path are refused.

    Every later read and copy uses the returned handle, never the path again.
    """
    parts = (turn_id, "notes", name)
    try:
        file = os.fdopen(_open_pinned(parts), "rb")
    except OSError:
        return None
    info = os.fstat(file.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:  # a hard link can name any file
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


def _open_dirs_creating(parts):
    """A descriptor for the folder under TRACES_PATH, made one step at a time; no step follows a link."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    dir_fd = os.open(TRACES_PATH, os.O_RDONLY | os.O_DIRECTORY)
    opened = False
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o777, dir_fd=dir_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        opened = True
        return dir_fd
    finally:
        if not opened:
            os.close(dir_fd)


class NoteTooLarge(OSError):
    """The note is over NOTE_LIMIT; it is not carried (a cut table would mislead the agent)."""


def _write_note(fd, file, info):
    """Write at most NOTE_LIMIT bytes. Reading one byte more catches a note that grew while copying."""
    with os.fdopen(fd, "wb") as out:
        data = file.read(NOTE_LIMIT + 1)
        if len(data) > NOTE_LIMIT:
            raise NoteTooLarge(f"note is over {NOTE_LIMIT} bytes")
        shutil.copyfileobj(io.BytesIO(data), out)
        out.flush()
        os.utime(out.fileno(), ns=(info.st_atime_ns, info.st_mtime_ns))


def _copy_note(file, parts):
    """Write a new file beside the target, then rename it over the target.

    Rename replaces the directory entry, so a link or hard link planted there is never written through.
    """
    info = os.fstat(file.fileno())
    name = parts[-1]
    temporary = f".{name}.{uuid.uuid4().hex}"
    dir_fd = _open_dirs_creating(parts[:-1])
    done = False
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
        )
        try:
            _write_note(fd, file, info)
            os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            done = True
        finally:
            if not done:
                os.unlink(temporary, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def carry_results(store, identity, turn):
    """Copy the latest earlier results.md into the new turn; return the context its goal needs.

    POSIX only: where no-follow descriptor opens are missing (Windows) this returns None before
    touching any file, and the turn runs without the previous results.
    """
    if not _carry_supported():
        logger.info(
            "Follow-up results are carried on POSIX only; turn %s runs without them",
            turn["turn_id"],
        )
        return None
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
        try:
            TRACES_PATH.mkdir(parents=True, exist_ok=True)
            _copy_note(file, (turn["turn_id"], "notes", "results.md"))
        except OSError:
            logger.warning(
                "Could not carry results.md into turn %s", turn["turn_id"], exc_info=True
            )
            return None
    return {"results": results, "report": _read_note(previous["turn_id"], "output.md")}


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
                except (OSError, sqlite3.Error, ValueError, KeyError, AttributeError, TypeError):
                    # Carrying results is best effort: the turn must still run and end.
                    # Named errors only: a broad handler would raise the quality-ratchet baseline.
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
