"""NACK arbitration probes from the CHE-1128 re-review (signing verifier x99-codex-sol).

Adopted as regression tests: a controlled worker thread pauses inside the real
DeviceExecutionLock.acquire while the real server NACK path runs.
"""

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services.host_admission import HostState, host_admission
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import DeviceExecutionLock, device_lock, trace_store


@pytest_asyncio.fixture(autouse=True)
async def isolated_queue(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(device_lock, "get_temp_dir", lambda _sub=None: lock_dir)
    monkeypatch.setattr(session_repo, "db_path", tmp_path / "sessions.db")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(TaskQueueService, "_concurrency_limit", classmethod(lambda cls: 0))
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.cancelled_session_ids.clear()
    state.draining = False
    TaskQueueService._run_tasks.clear()
    TaskQueueService._run_tasks_by_session.clear()
    host_admission.reset()
    yield
    tasks = list(TaskQueueService._run_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    host_admission.reset()


def host(host_id="host-a", max_runs=1):
    host_admission.heartbeat(host_id, HostState.ACTIVE, max_runs)
    for device in ("d1", "d2"):
        host_admission.share_device(host_id, device)


def row(session_id, device="d1", ticket=None):
    return {
        "session_id": session_id,
        "goal": "probe",
        "profile": "flash",
        "status": "pending",
        "device_serial": device,
        "host_id": "host-a",
        "ingress": "sdk",
        "queue_ticket": ticket,
    }


async def spawned_worker(tmp_path, monkeypatch, ticket, exited):
    worker = MagicMock(pid=os.getpid(), returncode=None)
    snapshot = SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")
    store = SimpleNamespace(snapshot_for_spawn=AsyncMock(return_value=snapshot))
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=worker))
    monkeypatch.setattr(TaskQueueService, "_start_output_forwarder", lambda *_args: None)

    async def wait_for_exit(_worker):
        await exited.wait()
        return worker.returncode

    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", wait_for_exit)
    state.queue_items.append(row("r1", ticket=ticket))
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.sleep(0)
    assert "r1" in state.active_runs
    return worker


def worker_lock(ticket):
    return DeviceExecutionLock(
        description="worker",
        device_id="d1",
        session_id="r1",
        queue_ticket=ticket,
        lock_scope="host:host-a",
        concurrency_mode="per_device",
    )


def _ticket_files(ticket):
    queue_dir = device_lock.get_temp_dir("device-locks") / "artemis-global-device.queue"
    return list(queue_dir.glob(f"*-{ticket}.wait"))


class ControlledAcquisition:
    """Hold a real, blocking worker acquire at named points inside the lock take.

    Like the real worker, it waits in the FIFO queue until it owns the device.
    """

    def __init__(self, lock, monkeypatch):
        self.lock = lock
        self.before_take = threading.Event()
        self.allow_take = threading.Event()
        self.attempted = threading.Event()
        self.allow_return = threading.Event()
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.took = False
        self.errors: list[BaseException] = []
        real_take = lock._try_acquire_owner_lock

        def controlled_take():
            self.before_take.set()
            assert self.allow_take.wait(5)
            self.took = real_take()
            self.attempted.set()
            assert self.allow_return.wait(5)
            return self.took

        monkeypatch.setattr(lock, "_try_acquire_owner_lock", controlled_take)
        self.thread = threading.Thread(target=self._acquire)

    def _acquire(self):
        try:
            self.lock.acquire(cancel_event=self.cancel)
        except BaseException as error:  # noqa: BLE001 - recorded for the assertions
            self.errors.append(error)
        finally:
            self.done.set()

    def start(self):
        self.thread.start()
        assert self.before_take.wait(5)

    def stop(self):
        self.cancel.set()
        self.allow_take.set()
        self.allow_return.set()
        self.thread.join(5)
        assert not self.thread.is_alive()
        self.lock.release()


@pytest.mark.asyncio
async def test_nack_landing_while_a_worker_is_mid_acquisition_kills_it_only_without_the_lock(
    tmp_path, monkeypatch
):
    """Re-review probe A: the worker is released into the acquisition mid-NACK.

    The server holds the device lock for the whole NACK, so the worker's attempt
    fails; the kill lands only after that, and the worker never owns the device.
    """
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    acquisition = ControlledAcquisition(lock, monkeypatch)
    acquisition.start()
    kills_while_locked: list[bool] = []

    def kill():
        acquisition.allow_take.set()  # the worker's take lands inside the NACK
        assert acquisition.attempted.wait(5)
        kills_while_locked.append(lock._acquired)

    worker.kill.side_effect = kill
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        ticket_kept = len(_ticket_files(ticket))  # before the test's own thread gives up
    finally:
        acquisition.stop()

    assert requeued is True
    assert ticket_kept == 1
    assert kills_while_locked == [False]
    assert acquisition.took is False  # the worker's take failed against the server's hold
    assert DeviceExecutionLock.get_active_owner("d1", "host:host-a") is None
    assert state.queue_items[0]["status"] == "pending"
    assert session_repo.get_session_by_id("r1")["status"] == "queued"


@pytest.mark.asyncio
async def test_nack_after_the_worker_took_the_lock_is_refused_and_kills_nothing(
    tmp_path, monkeypatch
):
    """The mirror order: the worker owns the device before the NACK arrives."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    acquisition = ControlledAcquisition(lock, monkeypatch)
    acquisition.start()
    acquisition.allow_take.set()
    assert acquisition.attempted.wait(5)  # owns the lock, has not returned from acquire yet
    assert acquisition.took
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        acquisition.allow_return.set()
        assert acquisition.done.wait(5)
    finally:
        acquisition.stop()

    assert requeued is False
    worker.kill.assert_not_called()
    assert acquisition.errors == []  # the worker went on to execute
    assert state.queue_items[0]["status"] == "starting"
    assert session_repo.get_session_by_id("r1")["status"] == "queued"


@pytest.mark.asyncio
async def test_nacked_run_stays_queued_with_its_ticket_even_if_the_worker_then_exits(
    tmp_path, monkeypatch
):
    """Re-review probe B: a start the host refused is never settled as failed."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    exited = asyncio.Event()
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, exited)
    run_task = TaskQueueService._run_tasks_by_session["r1"]

    assert await TaskQueueService.requeue_starting("r1") is True
    worker.returncode = 1  # the killed worker's exit must not settle the session
    exited.set()
    await asyncio.gather(run_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert session_repo.get_session_by_id("r1")["status"] == "queued"
    assert [(i["session_id"], i["status"]) for i in state.queue_items] == [("r1", "pending")]
    assert len(_ticket_files(ticket)) == 1


@pytest.mark.asyncio
async def test_nack_is_refused_when_the_arbiter_cannot_read_lock_state(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    monkeypatch.setattr(
        DeviceExecutionLock, "try_hold", MagicMock(side_effect=OSError("unreadable"))
    )

    assert await TaskQueueService.requeue_starting("r1") is False

    worker.kill.assert_not_called()
    assert state.queue_items[0]["status"] == "starting"
    assert len(_ticket_files(ticket)) == 1
