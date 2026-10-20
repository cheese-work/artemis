import asyncio
import json
import logging
import os
import shutil
import sqlite3
import stat
import threading
import time

from artemis.config import TRACES_PATH
from artemis.data_engine.chat_sessions import TERMINAL
from artemis.runtime.cancel_requests import current_process_created_at, is_cancel_requested

logger = logging.getLogger(__name__)


NOTE_LIMIT = 64 * 1024


def _read_note(turn_id, name):
    """A turn's note as (text, path), or None; never follows a link and reads at most 64 KB."""
    path = TRACES_PATH / turn_id / "notes" / name
    if path.is_symlink():
        return None
    try:
        # Windows has no O_NOFOLLOW; is_symlink above is the check there.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                return None
            return file.read(NOTE_LIMIT).decode("utf-8", errors="ignore"), path
    except OSError:
        return None


def carry_results(store, identity, turn):
    """Copy the latest earlier results.md into the new turn; return the context its goal needs."""
    earlier = [t for t in store.get(identity)["turns"] if t["number"] < turn["number"]]
    for previous in reversed(earlier):
        found = _read_note(previous["turn_id"], "results.md")
        if found:
            break
    else:
        return None
    results, source = found
    target = TRACES_PATH / turn["turn_id"] / "notes"
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / "results.md")
    except OSError:
        logger.warning("Could not carry results.md into turn %s", turn["turn_id"], exc_info=True)
        return None
    report = _read_note(earlier[-1]["turn_id"], "output.md")
    return {"results": results, "report": report[0] if report else None}


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
