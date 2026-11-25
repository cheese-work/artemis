"""CHE-1098 stage 4b: a host-bound run waits up to 10 s for the host recorder, then starts anyway."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services.host_admission import HostState, host_admission
from apps.admin_console.services.recording_assignments import RecordingAssignmentStore
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import device_lock, trace_store


class FakeTime:
    """One clock for the store's monotonic time and sleep: waiting costs no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.on_sleep()


@pytest_asyncio.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(device_lock, "get_temp_dir", lambda _sub=None: lock_dir)
    monkeypatch.setattr(session_repo, "db_path", tmp_path / "sessions.db")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    monkeypatch.delenv("ARTEMIS_HOST_RECORDING", raising=False)
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(TaskQueueService, "_concurrency_limit", classmethod(lambda cls: 0))
    monkeypatch.setattr(TaskQueueService, "_deliver_outcome", MagicMock())
    monkeypatch.setattr(TaskQueueService, "_recover_or_fail_recording", AsyncMock())
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.cancelled_session_ids.clear()
    state.draining = False
    host_admission.reset()
    host_admission.heartbeat("host-a", HostState.ACTIVE, 1)
    host_admission.share_device("host-a", "d1")
    yield
    host_admission.reset()
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()


@pytest.fixture
def clock(tmp_path, monkeypatch) -> FakeTime:
    fake = FakeTime()
    store = RecordingAssignmentStore(
        tmp_path / "hosts.db", monotonic=fake.monotonic, sleep=fake.sleep
    )
    monkeypatch.setattr(TaskQueueService, "_recording_store", classmethod(lambda cls: store))
    fake.store = store
    return fake


@pytest.fixture
def capture(monkeypatch) -> MagicMock:
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(run_catalog_repo, "set_recording_state", mock)
    return mock


def _states(capture: MagicMock) -> list[str]:
    return [call.kwargs["capture"] for call in capture.call_args_list]


async def _run(tmp_path, monkeypatch, *, host_id="host-a", spawn_error=None):
    """Execute one run with a fake worker; return (spawn mock, terminal status writes)."""
    snapshot = SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")
    config = SimpleNamespace(snapshot_for_spawn=AsyncMock(return_value=snapshot))
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: config)
    worker = MagicMock(pid=1, returncode=0)
    spawn = AsyncMock(return_value=worker, side_effect=spawn_error)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(TaskQueueService, "_start_output_forwarder", lambda *_args: None)
    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", AsyncMock(return_value=0))
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    item = {
        "session_id": "r1",
        "goal": "probe",
        "profile": "flash",
        "status": "starting",
        "device_serial": "d1",
        "ingress": "sdk",
        **({"host_id": host_id} if host_id else {}),
    }
    state.queue_items.append(item)
    await TaskQueueService._execute_task_item(item)
    return spawn, item


@pytest.mark.asyncio
async def test_flag_off_spawns_with_no_assignment_and_no_recording_state(
    tmp_path, monkeypatch, clock, capture
):
    spawn, _ = await _run(tmp_path, monkeypatch)

    spawn.assert_awaited_once()
    capture.assert_not_called()
    assert clock.store.reconcile("host-a", 0.0) == []
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_ready_signal_before_timeout_records_and_then_spawns(
    tmp_path, monkeypatch, clock, capture
):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")
    spawned_when = []

    def ready_on_second_poll():
        if clock.now >= 0.4:
            (a,) = clock.store.reconcile("host-a", 0.0)
            clock.store.mark_ready("host-a", a.recording_id, 1, 1000.0, 5.0)
            clock.on_sleep = lambda: None

    clock.on_sleep = ready_on_second_poll
    spawn, item = await _run(tmp_path, monkeypatch)
    spawned_when.append(clock.now)

    spawn.assert_awaited_once()
    assert _states(capture) == ["pending", "recording"]
    assert 0.4 <= spawned_when[0] < 10.0
    assert item["worker_spawned"] is True


@pytest.mark.asyncio
async def test_no_ready_signal_is_missing_not_ready_at_10s_and_the_run_still_spawns(
    tmp_path, monkeypatch, clock, capture, caplog
):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")
    caplog.set_level("INFO")

    spawn, _ = await _run(tmp_path, monkeypatch)

    spawn.assert_awaited_once()
    assert clock.now == pytest.approx(10.0)
    assert _states(capture) == ["pending", "missing:not_ready"]
    assert "event=recorder_not_ready session_id=r1 host_id=host-a" in caplog.text
    assert session_repo.get_session_by_id("r1")["status"] != "failed"


@pytest.mark.asyncio
async def test_run_without_host_id_gets_no_assignment_even_with_the_flag_on(
    tmp_path, monkeypatch, clock, capture
):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")

    spawn, _ = await _run(tmp_path, monkeypatch, host_id=None)

    spawn.assert_awaited_once()
    capture.assert_not_called()
    assert clock.store.reconcile("host-a", 0.0) == []


@pytest.mark.asyncio
async def test_flag_needs_the_host_agent_flag_too(tmp_path, monkeypatch, clock, capture):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")
    monkeypatch.delenv("ARTEMIS_HOST_AGENT")

    spawn, _ = await _run(tmp_path, monkeypatch)

    spawn.assert_awaited_once()
    capture.assert_not_called()


@pytest.mark.asyncio
async def test_worker_exit_requests_stop_at_revision_2(tmp_path, monkeypatch, clock, capture):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")

    await _run(tmp_path, monkeypatch)

    (stopped,) = clock.store.reconcile("host-a", 0.0)
    assert (stopped.desired_state, stopped.revision) == ("stop", 2)


@pytest.mark.asyncio
async def test_spawn_failure_requests_stop(tmp_path, monkeypatch, clock, capture):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")

    await _run(tmp_path, monkeypatch, spawn_error=OSError("no worker"))

    (stopped,) = clock.store.reconcile("host-a", 0.0)
    assert (stopped.desired_state, stopped.revision) == ("stop", 2)


@pytest.mark.asyncio
async def test_nack_during_the_wait_does_not_stop_and_the_retry_reuses_the_recording(
    tmp_path, monkeypatch, clock, capture
):
    monkeypatch.setenv("ARTEMIS_HOST_RECORDING", "1")
    snapshot = SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")
    config = SimpleNamespace(snapshot_for_spawn=AsyncMock(return_value=snapshot))
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: config)
    spawn = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    release = asyncio.Event()
    in_wait = asyncio.Event()
    loop = asyncio.get_running_loop()

    def block_in_wait():
        loop.call_soon_threadsafe(in_wait.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(5)

    clock.on_sleep = block_in_wait
    ticket = device_lock.DeviceExecutionLock.reserve(
        "probe", "d1", session_id="r1", lock_scope="host:host-a"
    )
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    item = {
        "session_id": "r1",
        "goal": "probe",
        "profile": "flash",
        "status": "pending",
        "device_serial": "d1",
        "host_id": "host-a",
        "ingress": "sdk",
        "queue_ticket": ticket,
    }
    state.queue_items.append(item)
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.wait_for(in_wait.wait(), 5)
    (first,) = clock.store.reconcile("host-a", 0.0)

    assert await TaskQueueService.requeue_starting("r1") is True
    release.set()
    await asyncio.sleep(0.1)

    spawn.assert_not_called()
    (after_nack,) = clock.store.reconcile("host-a", 0.0)
    assert (after_nack.desired_state, after_nack.revision) == ("record", 1)
    assert clock.store.assign("host-a", "r1", 1).recording_id == first.recording_id
