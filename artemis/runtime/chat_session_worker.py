import asyncio
import json
import logging
import os
import sqlite3
import threading
import time

from artemis.data_engine.chat_sessions import TERMINAL
from artemis.runtime.cancel_requests import current_process_created_at, is_cancel_requested

logger = logging.getLogger(__name__)


async def run_chat_session(store, identity, lease, execute, *, idle_seconds=300, poll_seconds=0.25):
    pid = os.getpid()
    store.claim_worker(identity, pid, current_process_created_at())
    cancel_event = threading.Event()
    acquired = False
    deadline = None
    turn_id = None
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
                turn["history"] = json.dumps(store.get(identity)["messages"], ensure_ascii=False)
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
        if acquired:
            await asyncio.to_thread(lease.release)
        store.release_worker(identity, pid)
