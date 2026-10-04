"""Run-outcome ownership at the server API seam (CHE-1089).

One outcome per session across worker exit, loss, completion, cancel and
restart; reads never repair state; the interrupted state and its typed reason
reach API clients.
"""

import asyncio
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
    TaskQueueService._emitted_event_ids.clear()
    yield db_path, events
    if subscriber in state.ipc_subscribers:  # on_shutdown clears the list itself
        state.ipc_subscribers.remove(subscriber)
    state.queue_items.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


def _add_running_session(db_path, pid=DEAD_PID, with_status_file=False) -> str:
    session_id = str(uuid.uuid4())
    assert session_repo.create_queued_session(session_id, "goal", "flash", "emulator-5554")
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


@pytest.mark.asyncio
async def test_crash_before_delivery_is_recovered_by_the_next_drain(env, monkeypatch):
    db_path, events = env
    session_id = _add_running_session(db_path)
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.BRIDGE_CLOSED)

    original = TaskQueueService._broadcast_event.__func__

    def crash(cls, event_type, data):
        raise RuntimeError("process died mid-broadcast")

    monkeypatch.setattr(TaskQueueService, "_broadcast_event", classmethod(crash))
    TaskQueueService._deliver_outcome(session_id, {}, "goal")
    assert events == [] and _outbox_pending(db_path) == 1

    monkeypatch.setattr(TaskQueueService, "_broadcast_event", classmethod(original))
    TaskQueueService._drain_outcome_events()

    assert [p["event_id"] for p in _emitted(events, session_id)] == [f"{session_id}:outcome"]
    assert len(_interrupted(events, session_id)) == 1
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_crash_after_delivery_redelivers_with_the_same_event_id(env, monkeypatch):
    db_path, events = env
    session_id = _add_running_session(db_path)
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.BRIDGE_CLOSED)

    def die(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(LifecycleAuthority, "acknowledge", die)
        TaskQueueService._deliver_outcome(session_id, {}, "goal")
    assert len(_emitted(events, session_id)) == 1 and _outbox_pending(db_path) == 1

    # Same process: the in-memory memo suppresses the duplicate broadcast.
    TaskQueueService._drain_outcome_events()
    assert len(_emitted(events, session_id)) == 1 and _outbox_pending(db_path) == 0

    # After a restart the memo is gone: a redelivery reuses the dedupe id.
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.AUTH_EXPIRED)  # no-op
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE lifecycle_outbox SET delivered_at = NULL")
    TaskQueueService._emitted_event_ids.clear()
    TaskQueueService._drain_outcome_events()
    ids = [p["event_id"] for p in _emitted(events, session_id)]
    assert ids == [f"{session_id}:outcome"] * 2
    assert _outbox_pending(db_path) == 0


@pytest.mark.asyncio
async def test_the_periodic_sweep_drains_events_left_pending(env):
    db_path, events = env
    session_id = _add_running_session(db_path)
    LifecycleAuthority(db_path).interrupt(session_id, InterruptReason.SERVER_RESTARTED)
    assert _outbox_pending(db_path) == 1

    TaskQueueService._reap_vanished_workers()

    assert len(_emitted(events, session_id)) == 1
    assert _outbox_pending(db_path) == 0
