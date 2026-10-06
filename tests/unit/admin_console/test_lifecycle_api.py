"""Run-outcome ownership at the server API seam (CHE-1089).

One outcome per session across worker exit, loss, completion, cancel and
restart; reads never repair state; the interrupted state and its typed reason
reach API clients.
"""

import asyncio
import json
import sqlite3
import uuid
from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import sessions as sessions_router
from apps.admin_console.server import app
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import trace_store
from artemis.runtime.lifecycle import InterruptReason, LifecycleAuthority

DEAD_PID = 2**22 + 12345


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(session_repo, "db_path", db_path)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(sessions_router.media_service, "build_video_index", lambda: {})
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", lambda *_args, **_kw: None
    )
    monkeypatch.setattr(session_repo, "process_is_alive", lambda pid: pid != DEAD_PID)
    events: list[tuple[str, dict]] = []
    subscriber = lambda event_type, data: events.append((event_type, data))  # noqa: E731
    state.ipc_subscribers.append(subscriber)
    state.queue_items.clear()
    state.active_connections.clear()
    yield db_path, events
    if subscriber in state.ipc_subscribers:  # on_shutdown clears the list itself
        state.ipc_subscribers.remove(subscriber)
    state.queue_items.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


def _add_running_session(db_path, pid=DEAD_PID, with_status_file=False, notify_context=None) -> str:
    session_id = str(uuid.uuid4())
    assert session_repo.create_queued_session(
        session_id, "goal", "flash", "emulator-5554", None, notify_context
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET status = 'running', pid = ? WHERE session_id = ?",
            (pid, session_id),
        )
    if with_status_file:
        trace_store.init_trace(session_id, "goal", "flash")
    return session_id


def _snapshot(db_path) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        sessions = conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
        outbox = conn.execute("SELECT * FROM lifecycle_outbox").fetchall()
    return [tuple(sessions), tuple(outbox)]


def _ended(events, session_id):
    return [e for e in events if e[0] == "session_ended" and e[1]["session_id"] == session_id]


def _interrupted(events, session_id):
    return [e for e in events if e[0] == "run_interrupted" and e[1]["session_id"] == session_id]


# -- reads never mutate ----------------------------------------------------


@pytest.mark.asyncio
async def test_listing_sessions_never_repairs_state(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)
    before_db = _snapshot(db_path)
    before_status = trace_store.read_status(session_id)

    async with _client() as client:
        first = await client.get("/api/sessions")
        second = await client.get("/api/sessions")
        detail = await client.get(f"/api/sessions/{session_id}")

    assert first.status_code == second.status_code == detail.status_code == 200
    assert _snapshot(db_path) == before_db
    assert trace_store.read_status(session_id) == before_status
    assert [r["status"] for r in first.json()] == ["running"]
    assert events == []


# -- interrupted reaches clients -------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_state_and_typed_reason_are_served(env):
    db_path, _ = env
    session_id = _add_running_session(db_path)
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.DEVICE_OFFLINE)

    async with _client() as client:
        listed = (await client.get("/api/sessions")).json()
        detail = (await client.get(f"/api/sessions/{session_id}")).json()

    assert [(r["status"], r["interrupt_reason"]) for r in listed] == [
        ("interrupted", "device_offline")
    ]
    assert (detail["status"], detail["interrupt_reason"]) == ("interrupted", "device_offline")


# -- restart ---------------------------------------------------------------


def test_server_restart_marks_running_sessions_interrupted(env):
    db_path, _ = env
    gone = _add_running_session(db_path, with_status_file=True)
    alive = _add_running_session(db_path, pid=4242)

    assert session_repo.cleanup_orphans_on_startup() == 1

    row = session_repo.get_session_by_id(gone)
    assert (row["status"], row["interrupt_reason"]) == ("interrupted", "server_restarted")
    assert trace_store.read_status(gone)["status"] == "interrupted"
    assert trace_store.read_status(gone)["interrupt_reason"] == "server_restarted"
    assert session_repo.get_session_by_id(alive)["status"] == "running"


# -- interleavings through the queue service -------------------------------


def _announce(session_id, *_ignored):
    TaskQueueService._deliver_outcome(session_id, {}, "goal")


@pytest.mark.asyncio
async def test_cancel_then_worker_exit_publishes_one_cancelled_event(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)

    session_repo.update_session_status(session_id, "cancelled")
    TaskQueueService._finalize_targeted_stop(session_id, None, None, False, None, None)
    status = await TaskQueueService._persist_terminal_session_status(session_id, 1, False)
    _announce(session_id, status)

    assert status == "cancelled"
    assert session_repo.get_session_by_id(session_id)["status"] == "cancelled"
    assert trace_store.read_status(session_id)["status"] == "cancelled"
    assert [e[1]["status"] for e in _ended(events, session_id)] == ["cancelled"]


@pytest.mark.asyncio
async def test_completion_beats_a_later_stop_and_a_later_loss(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)
    LifecycleAuthority(db_path).finish(session_id, "completed")

    session_repo.update_session_status(session_id, "cancelled")
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.HOST_DISCONNECTED)
    status = await TaskQueueService._persist_terminal_session_status(session_id, 1, True)
    _announce(session_id, status)

    assert status == "completed"
    assert trace_store.read_status(session_id)["status"] == "completed"
    assert [e[1]["status"] for e in _ended(events, session_id)] == ["completed"]
    assert _interrupted(events, session_id) == []


@pytest.mark.asyncio
async def test_device_loss_then_worker_failure_is_one_interrupted_event(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)

    # The worker commits interrupted(device_offline) itself, then exits non-zero.
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.DEVICE_OFFLINE)
    status = await TaskQueueService._persist_terminal_session_status(session_id, 1, False)
    _announce(session_id, status)
    _announce(session_id, status)  # a duplicate finalizer must not re-announce

    assert status == "interrupted"
    row = session_repo.get_session_by_id(session_id)
    assert (row["status"], row["interrupt_reason"], row["exit_cause"]) == (
        "interrupted",
        "device_offline",
        "exit:1",
    )
    assert len(_ended(events, session_id)) == 1
    assert _ended(events, session_id)[0][1]["interrupt_reason"] == "device_offline"
    assert len(_interrupted(events, session_id)) == 1
    assert _interrupted(events, session_id)[0][1]["interrupt_reason"] == "device_offline"


@pytest.mark.asyncio
async def test_concurrent_finalizers_publish_exactly_one_outcome(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)

    async def worker_exit():
        status = await TaskQueueService._persist_terminal_session_status(session_id, 1, False)
        _announce(session_id, status)

    async def stop():
        await asyncio.to_thread(session_repo.update_session_status, session_id, "cancelled")
        _announce(session_id, "cancelled", True)

    async def loss():
        await asyncio.to_thread(
            LifecycleAuthority(db_path).interrupt, session_id, InterruptReason.BRIDGE_CLOSED
        )
        _announce(session_id, "interrupted")

    await asyncio.gather(worker_exit(), stop(), loss())

    final = session_repo.get_session_by_id(session_id)["status"]
    assert len(_ended(events, session_id)) == 1
    assert _ended(events, session_id)[0][1]["status"] == final
    assert trace_store.read_status(session_id)["status"] == final
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_outbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_vanished_worker_sweep_is_a_writer_path_not_a_read(env):
    db_path, events = env
    session_id = _add_running_session(db_path, with_status_file=True)

    swept = TaskQueueService._reap_vanished_workers()

    assert swept == [session_id]
    assert session_repo.get_session_by_id(session_id)["status"] == "failed"
    assert trace_store.read_status(session_id)["status"] == "failed"
    assert len(_ended(events, session_id)) == 1


# -- DataEngine no longer writes status independently ----------------------


def test_data_engine_end_session_cannot_overwrite_a_cancel(tmp_path, monkeypatch):
    from unittest.mock import MagicMock as _Mock

    from artemis.data_engine.engine import DataEngine
    from artemis.context import ArtemisContext

    ctx = _Mock(spec=ArtemisContext)
    ctx.execution_setup = _Mock(traces_path=str(tmp_path))
    ctx.device = None
    engine = DataEngine(ctx)
    session_id = engine.start_session("goal")

    LifecycleAuthority(engine.storage.db_path).finish(str(session_id), "cancelled")
    engine.update_session_device_info(note="refresh after cancel")
    engine.end_session("completed")

    session = engine.storage.get_session(session_id)
    assert session.status == "cancelled"
    assert session.device_info["note"] == "refresh after cancel"


def test_data_engine_end_session_records_interrupt_reason(tmp_path):
    from artemis.data_engine.engine import DataEngine
    from artemis.context import ArtemisContext

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = MagicMock(traces_path=str(tmp_path))
    ctx.device = None
    engine = DataEngine(ctx)
    session_id = engine.start_session("goal")

    engine.end_session("interrupted", interrupt_reason=InterruptReason.DEVICE_OFFLINE)
    engine.end_session("failed")

    with sqlite3.connect(engine.storage.db_path) as conn:
        row = conn.execute(
            "SELECT status, interrupt_reason FROM sessions WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        outbox = conn.execute("SELECT COUNT(*) FROM lifecycle_outbox").fetchone()[0]
    assert tuple(row) == ("interrupted", "device_offline")
    assert outbox == 1


# -- restart/shutdown interruptions are delivered through the real paths -----


@pytest.fixture
def server_stubs(monkeypatch):
    """Stub only what the real startup/shutdown hooks reach outside the lifecycle."""
    from unittest.mock import AsyncMock, MagicMock

    from apps.admin_console import server

    calls = MagicMock()
    for name in ("write_server_info", "start_awake_service", "shutdown_awake_service"):
        monkeypatch.setattr(server, name, getattr(calls, name))
    monkeypatch.setattr(server, "clear_server_info", calls.clear_server_info)
    monkeypatch.setattr(server.DeviceExecutionLock, "cleanup_stale_locks", lambda *a: 0)
    monkeypatch.setattr(server.device_pool, "warm_up_async", AsyncMock(return_value=True))
    monkeypatch.setattr(server.ipc_service, "start_server", AsyncMock())
    calls.stop_server_async = AsyncMock()
    monkeypatch.setattr(server.ipc_service, "stop_server", calls.stop_server_async)
    for name in (
        "archive_older_replays_on_launch",
        "verify_chunks_exist_on_launch",
        "recover_orphaned_recordings_on_launch",
    ):
        monkeypatch.setattr(server.task_queue_service, name, lambda *a, **k: None)
    monkeypatch.setattr(server.task_queue_service, "queue_worker", AsyncMock())
    return server, calls


def _outbox_pending(db_path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM lifecycle_outbox WHERE delivered_at IS NULL"
        ).fetchone()[0]


@pytest.mark.asyncio
async def test_real_startup_delivers_and_drains_restart_interruptions(env, server_stubs):
    db_path, events = env
    server, _calls = server_stubs
    session_id = _add_running_session(db_path, with_status_file=True)

    await server.on_startup()
    await asyncio.sleep(0)

    assert [e[1]["status"] for e in _ended(events, session_id)] == ["interrupted"]
    assert _ended(events, session_id)[0][1]["interrupt_reason"] == "server_restarted"
    assert [e[1]["interrupt_reason"] for e in _interrupted(events, session_id)] == [
        "server_restarted"
    ]
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_real_shutdown_delivers_and_drains_its_interruptions(env, server_stubs):
    db_path, events = env
    server, calls = server_stubs
    session_id = _add_running_session(db_path, pid=4242, with_status_file=True)
    state.queue_items[:] = [{"session_id": session_id, "status": "running"}]
    state.worker_task = None

    await server.on_shutdown()

    assert session_repo.get_session_by_id(session_id)["interrupt_reason"] == "server_restarted"
    assert len(_ended(events, session_id)) == 1
    assert len(_interrupted(events, session_id)) == 1
    assert _outbox_pending(db_path) == 0
    calls.stop_server_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_locked_database_cannot_skip_the_rest_of_shutdown(env, server_stubs, monkeypatch):
    db_path, events = env
    server, calls = server_stubs
    session_id = _add_running_session(db_path, pid=4242)
    state.queue_items[:] = [{"session_id": session_id, "status": "running"}]
    state.worker_task = None

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(LifecycleAuthority, "interrupt", locked)
    monkeypatch.setattr(LifecycleAuthority, "pending_events", locked)

    await server.on_shutdown()

    calls.stop_server_async.assert_awaited_once()
    calls.shutdown_awake_service.assert_called_once()
    calls.clear_server_info.assert_called_once()
    assert events and all(e[0] == "server_shutdown" for e in events)


# -- the outbox survives crashes around delivery ---------------------------


def _emitted(events, session_id):
    return [e[1] for e in _ended(events, session_id)]


def _interrupt(db_path, session_id, reason=InterruptReason.BRIDGE_CLOSED):
    LifecycleAuthority(db_path).interrupt(session_id, reason)


@pytest.mark.asyncio
async def test_process_exit_before_the_send_is_still_delivered_after_restart(env):
    import subprocess
    import sys

    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    child_code = """
import os, sys
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.services import task_queue_service as queue_module
from apps.admin_console.services.task_queue_service import TaskQueueService
queue_module.session_repo = SessionRepository(sys.argv[1])
def die_before_the_send(cls, *args):
    os._exit(79)
TaskQueueService._broadcast_outcome = classmethod(die_before_the_send)
TaskQueueService._deliver_outcome(sys.argv[2], {}, "goal")
os._exit(80)
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(db_path), session_id],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 79, child.stderr
    assert events == [] and _outbox_pending(db_path) == 1

    TaskQueueService._drain_outcome_events()  # the restarted server

    assert len(_interrupted(events, session_id)) == 1
    assert len(_ended(events, session_id)) == 1
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_crash_after_broadcast_before_ack_delivers_exactly_once_across_a_restart(
    env, monkeypatch
):
    from unittest.mock import MagicMock

    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    notify = MagicMock(return_value=True)
    monkeypatch.setattr("mcp_server.notifiers.notify", notify)
    task_item = {"session_id": session_id, "conversation_id": "conv-1", "ingress": "mcp"}

    def die(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(LifecycleAuthority, "acknowledge", die)
        TaskQueueService._deliver_outcome(session_id, task_item, "goal")
    assert len(_interrupted(events, session_id)) == 1 and _outbox_pending(db_path) == 1

    TaskQueueService._forget_delivery_memory()  # a restart: nothing in memory survives
    TaskQueueService._drain_outcome_events()
    TaskQueueService._deliver_outcome(session_id, task_item, "goal")

    assert [p["event_id"] for p in _emitted(events, session_id)] == [f"{session_id}:outcome"]
    assert len(_interrupted(events, session_id)) == 1
    notify.assert_called_once()
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_a_failed_notification_is_retried_and_only_then_acknowledged(env, monkeypatch):
    from unittest.mock import MagicMock

    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    notify = MagicMock(side_effect=[False, True])
    monkeypatch.setattr("mcp_server.notifiers.notify", notify)
    task_item = {"session_id": session_id, "conversation_id": "conv-1", "ingress": "mcp"}

    TaskQueueService._deliver_outcome(session_id, task_item, "goal")
    assert notify.call_count == 1 and _outbox_pending(db_path) == 1  # False: not acknowledged

    TaskQueueService._deliver_outcome(session_id, task_item, "goal")

    assert notify.call_count == 2 and _outbox_pending(db_path) == 0
    # the UI broadcast already succeeded, so the retry does not repeat it
    assert len(_interrupted(events, session_id)) == 1
    assert len(_ended(events, session_id)) == 1


@pytest.mark.asyncio
async def test_a_failing_broadcast_does_not_delay_the_notification(env, monkeypatch):
    from unittest.mock import MagicMock

    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    notify = MagicMock(return_value=True)
    monkeypatch.setattr("mcp_server.notifiers.notify", notify)
    task_item = {"session_id": session_id, "conversation_id": "conv-1", "ingress": "mcp"}

    def broken(_event_type, _payload):
        raise RuntimeError("subscriber down")

    state.ipc_subscribers.append(broken)
    try:
        TaskQueueService._deliver_outcome(session_id, task_item, "goal")
    finally:
        state.ipc_subscribers.remove(broken)

    notify.assert_called_once()  # broadcast failed below the cap; notify still ran
    assert _outbox_pending(db_path) == 1  # the broadcast is still owed a retry


@pytest.mark.asyncio
async def test_a_subscriber_that_fails_once_is_retried_without_repeating_for_the_others(env):
    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    seen: list[str] = []
    flaky_calls = {"n": 0}

    def flaky(event_type, payload):
        if event_type == "run_interrupted":
            flaky_calls["n"] += 1
            if flaky_calls["n"] == 1:
                raise RuntimeError("consumer temporarily unavailable")
            seen.append(event_type)

    state.ipc_subscribers.append(flaky)
    try:
        TaskQueueService._deliver_outcome(session_id, {}, "goal")
        assert _outbox_pending(db_path) == 1  # the failure left it pending
        TaskQueueService._drain_outcome_events()
    finally:
        state.ipc_subscribers.remove(flaky)

    assert seen == ["run_interrupted"]  # the flaky subscriber got it on the retry
    assert len(_interrupted(events, session_id)) == 1  # the healthy one never saw a repeat
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_a_permanently_failing_subscriber_is_given_up_on_after_bounded_attempts(env):
    db_path, events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    attempts: list[str] = []

    def broken(event_type, payload):
        if event_type == "run_interrupted":
            attempts.append(event_type)
            raise RuntimeError("never works")

    state.ipc_subscribers.append(broken)
    try:
        for _ in range(20):
            TaskQueueService._drain_outcome_events()
    finally:
        state.ipc_subscribers.remove(broken)

    assert 1 < len(attempts) <= TaskQueueService._MAX_DELIVERY_ATTEMPTS
    assert len(_interrupted(events, session_id)) == 1  # healthy subscriber: exactly once
    assert _outbox_pending(db_path) == 0


_CRASH_CHILD = """
import json, os, sys
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.services import task_queue_service as queue_module
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import trace_store
from artemis.runtime.lifecycle import LifecycleAuthority
import mcp_server.notifiers as notifiers
from mcp_server.notifiers import CompositeNotifier, FileNotifier
db, session_id, sink, traces, mode = sys.argv[1:6]
queue_module.session_repo = SessionRepository(db)
trace_store.TRACES_DIR = traces
notifiers._default_notifier = CompositeNotifier([FileNotifier()])
def capture(event_type, payload):
    with open(sink, "a", encoding="utf-8") as out:
        out.write(json.dumps({"t": event_type, "id": payload.get("event_id")}) + "\\n")
        out.flush()
        os.fsync(out.fileno())
state.ipc_subscribers[:] = [capture]
original_mark = LifecycleAuthority.mark_delivered
def die_before_mark(self, event_id, consumer, **kw):
    if mode == "after_send_before_mark" and consumer == "broadcast":
        os._exit(79)
    if mode == "after_notify_before_mark" and consumer == "notify":
        os._exit(79)
    return original_mark(self, event_id, consumer, **kw)
LifecycleAuthority.mark_delivered = die_before_mark
item = {"session_id": session_id, "conversation_id": "conv-1", "ingress": "mcp"}
if mode == "restart":
    TaskQueueService._drain_outcome_events()
else:
    TaskQueueService._deliver_outcome(session_id, item, "goal")
"""


def _crash_run(tmp_path, db_path, session_id, mode):
    import subprocess
    import sys

    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_CHILD,
            str(db_path),
            session_id,
            str(tmp_path / "sink.jsonl"),
            str(tmp_path / "traces"),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _sink(tmp_path):
    path = tmp_path / "sink.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _notifications(tmp_path, session_id):
    path = tmp_path / "traces" / session_id / "notifications.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_real_crash_after_send_before_mark_leaves_one_effect_per_consumer(env, tmp_path):
    db_path, _events = env
    session_id = _add_running_session(
        db_path, notify_context={"conversation_id": "conv-1", "ingress": "mcp", "goal": "goal"}
    )
    _interrupt(db_path, session_id)

    first = _crash_run(tmp_path, db_path, session_id, "after_send_before_mark")
    assert first.returncode == 79, first.stderr
    assert [e["t"] for e in _sink(tmp_path)] == ["session_ended", "run_interrupted"]
    second = _crash_run(tmp_path, db_path, session_id, "restart")
    assert second.returncode == 0, second.stderr

    sink = _sink(tmp_path)
    assert sorted(e["t"] for e in sink) == ["run_interrupted", "session_ended"]  # no replay
    assert {e["id"] for e in sink} == {f"{session_id}:outcome"}
    recorded = LifecycleAuthority(db_path).events(session_id)
    assert sorted(e["event_type"] for e in recorded) == ["run_interrupted", "session_ended"]
    assert len(_notifications(tmp_path, session_id)) == 1  # the restart notified once
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_real_crash_after_notify_before_mark_writes_one_notification(env, tmp_path):
    db_path, _events = env
    session_id = _add_running_session(
        db_path, notify_context={"conversation_id": "conv-1", "ingress": "mcp", "goal": "goal"}
    )
    _interrupt(db_path, session_id)

    first = _crash_run(tmp_path, db_path, session_id, "after_notify_before_mark")
    assert first.returncode == 79, first.stderr
    assert len(_notifications(tmp_path, session_id)) == 1
    second = _crash_run(tmp_path, db_path, session_id, "restart")
    assert second.returncode == 0, second.stderr

    records = _notifications(tmp_path, session_id)
    assert len(records) == 1
    assert records[0]["payload"]["event_id"] == f"{session_id}:outcome"
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_restart_drain_notifies_from_the_persisted_context_with_no_queue_item(
    env, tmp_path, monkeypatch
):
    from unittest.mock import MagicMock

    db_path, _events = env
    session_id = _add_running_session(
        db_path, notify_context={"conversation_id": "conv-9", "ingress": "mcp", "goal": "Open X"}
    )
    _interrupt(db_path, session_id)
    notify = MagicMock(return_value=True)
    monkeypatch.setattr("mcp_server.notifiers.notify", notify)
    state.queue_items.clear()  # a restarted server remembers no queue item

    TaskQueueService._drain_outcome_events()

    notify.assert_called_once()
    assert notify.call_args.kwargs["conversation_id"] == "conv-9"
    assert "Open X" in notify.call_args.kwargs["message"]
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_enqueue_persists_the_notify_context(env):
    from unittest.mock import AsyncMock, patch

    db_path, _events = env
    with (
        patch.object(TaskQueueService, "ensure_worker_running"),
        patch.object(
            TaskQueueService, "_reject_unavailable_device", new=AsyncMock(return_value=None)
        ),
    ):
        result = await TaskQueueService.enqueue_tasks(
            ["Persist me"], device_serial="test-device", ingress="mcp", conversation_id="conv-3"
        )
    session_id = result["tasks"][0]["session_id"]

    assert LifecycleAuthority(db_path).get_notify_context(session_id) == {
        "conversation_id": "conv-3",
        "ingress": "mcp",
        "goal": "Persist me",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["notify", "broadcast"])
async def test_attempts_are_persisted_and_the_consumer_is_abandoned_durably(
    env, monkeypatch, consumer
):
    from unittest.mock import MagicMock

    db_path, events = env
    session_id = _add_running_session(
        db_path, notify_context={"conversation_id": "conv-1", "ingress": "mcp", "goal": "goal"}
    )
    _interrupt(db_path, session_id)
    notify = MagicMock(return_value=consumer != "notify")
    monkeypatch.setattr("mcp_server.notifiers.notify", notify)
    attempts: list[str] = []

    def broken(event_type, payload):
        attempts.append(event_type)
        raise RuntimeError("never works")

    if consumer == "broadcast":
        state.ipc_subscribers.append(broken)
    try:
        for _ in range(TaskQueueService._MAX_DELIVERY_ATTEMPTS + 3):
            if consumer == "notify":
                # every notify drain is a "restart": only the persisted count remains
                TaskQueueService._forget_delivery_memory()
            TaskQueueService._drain_outcome_events()
    finally:
        if broken in state.ipc_subscribers:
            state.ipc_subscribers.remove(broken)

    # the persisted cap stopped the retries and the row acknowledged
    assert _outbox_pending(db_path) == 0
    with sqlite3.connect(db_path) as conn:
        abandoned = conn.execute("SELECT abandoned FROM lifecycle_outbox").fetchone()[0]
    assert abandoned == consumer
    if consumer == "notify":
        assert notify.call_count == TaskQueueService._MAX_DELIVERY_ATTEMPTS
    else:
        assert len(attempts) <= 2 * TaskQueueService._MAX_DELIVERY_ATTEMPTS
        assert len(_interrupted(events, session_id)) == 1


@pytest.mark.asyncio
async def test_recorded_events_are_served_to_reconnecting_clients(env):
    db_path, _events = env
    session_id = _add_running_session(db_path)
    _interrupt(db_path, session_id)
    TaskQueueService._drain_outcome_events()

    async with _client() as client:
        served = (await client.get(f"/api/sessions/{session_id}/events")).json()

    assert sorted(e["event_type"] for e in served) == ["run_interrupted", "session_ended"]
    assert {e["event_id"] for e in served} == {f"{session_id}:outcome"}


@pytest.mark.asyncio
async def test_the_periodic_sweep_drains_events_left_pending(env):
    db_path, events = env
    session_id = _add_running_session(db_path)
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.SERVER_RESTARTED)
    assert _outbox_pending(db_path) == 1

    TaskQueueService._reap_vanished_workers()

    assert len(_emitted(events, session_id)) == 1
    assert _outbox_pending(db_path) == 0
