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


@pytest.mark.asyncio
async def test_kill_cannot_land_between_owner_acquisition_and_post_fence_check(
    tmp_path, monkeypatch
):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    reached_acquisition = threading.Event()
    allow_acquisition = threading.Event()
    owner_acquired = threading.Event()
    allow_post_check = threading.Event()
    worker_done = threading.Event()
    errors = []
    kills_while_locked = []
    real_take = lock._try_acquire_owner_lock

    def controlled_take():
        reached_acquisition.set()
        assert allow_acquisition.wait(5)
        acquired = real_take()
        assert acquired
        owner_acquired.set()
        assert allow_post_check.wait(5)
        return acquired

    monkeypatch.setattr(lock, "_try_acquire_owner_lock", controlled_take)

    def acquire():
        try:
            lock.acquire(blocking=False)
        except BaseException as error:
            errors.append(error)
        finally:
            worker_done.set()

    thread = threading.Thread(target=acquire)
    thread.start()
    assert reached_acquisition.wait(5)
    real_snapshot = TaskQueueService._held_lock_session_ids

    def snapshot_then_owner_acquires(_cls):
        observed = real_snapshot()
        assert "r1" not in observed
        allow_acquisition.set()
        assert owner_acquired.wait(5)
        assert lock._acquired
        return observed

    monkeypatch.setattr(
        TaskQueueService, "_held_lock_session_ids", classmethod(snapshot_then_owner_acquires)
    )

    def kill():
        kills_while_locked.append(lock._acquired)
        allow_post_check.set()
        assert worker_done.wait(5)

    worker.kill.side_effect = kill
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        print(f"FENCE GAP: requeued={requeued}, kills_while_locked={kills_while_locked}")
        assert True not in kills_while_locked
    finally:
        allow_acquisition.set()
        allow_post_check.set()
        thread.join(5)
        assert not thread.is_alive()
        lock.release()


@pytest.mark.asyncio
async def test_refused_nack_after_fence_abort_must_remain_queued_not_fail(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    exited = asyncio.Event()
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, exited)
    run_task = TaskQueueService._run_tasks_by_session["r1"]
    lock = worker_lock(ticket)
    owner_acquired = threading.Event()
    allow_post_check = threading.Event()
    marker_seen = threading.Event()
    allow_abort = threading.Event()
    worker_done = threading.Event()
    errors = []
    real_take = lock._try_acquire_owner_lock
    real_nacked = lock._nacked

    def controlled_take():
        taken = real_take()
        assert taken
        owner_acquired.set()
        assert allow_post_check.wait(5)
        return taken

    def controlled_nacked():
        observed = real_nacked()
        if lock._acquired and observed:
            marker_seen.set()
            assert allow_abort.wait(5)
        return observed

    monkeypatch.setattr(lock, "_try_acquire_owner_lock", controlled_take)
    monkeypatch.setattr(lock, "_nacked", controlled_nacked)

    def acquire():
        try:
            lock.acquire(blocking=False)
        except BaseException as error:
            errors.append(error)
        finally:
            worker_done.set()

    thread = threading.Thread(target=acquire)
    thread.start()
    assert owner_acquired.wait(5)
    real_snapshot = TaskQueueService._held_lock_session_ids

    def snapshot_after_worker_observes_marker(_cls):
        allow_post_check.set()
        assert marker_seen.wait(5)
        observed = real_snapshot()
        assert "r1" in observed
        return observed

    monkeypatch.setattr(
        TaskQueueService,
        "_held_lock_session_ids",
        classmethod(snapshot_after_worker_observes_marker),
    )
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        assert requeued is False
        worker.kill.assert_not_called()
        allow_abort.set()
        assert worker_done.wait(5)
        assert len(errors) == 1 and isinstance(errors[0], device_lock.DeviceBusyError)
        assert not lock._acquired
        worker.returncode = 1
        exited.set()
        await asyncio.gather(run_task)
        persisted = session_repo.get_session_by_id("r1")
        print(
            f"REFUSED NACK: requeued={requeued}, status={persisted['status']}, "
            f"queue_ids={[item['session_id'] for item in state.queue_items]}"
        )
        assert persisted["status"] == "queued"
        assert state.queue_items[0]["status"] == "pending"
    finally:
        allow_post_check.set()
        allow_abort.set()
        thread.join(5)
        assert not thread.is_alive()
        lock.release()
