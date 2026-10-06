"""Server NACK of a host start (CHE-1128), including the signing verifier's probes.

The NACK is honored only before a worker process exists; once one was or may have
been spawned it is refused and nothing is killed, whatever the lock state is.
Probes pause a real worker thread inside DeviceExecutionLock.acquire (including
metadata publication) while the real server NACK path runs.
"""

import asyncio
import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services.host_admission import HostState, host_admission
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import DeviceExecutionLock, device_lock, trace_store
from artemis.runtime.lifecycle import LifecycleAuthority


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
async def test_nack_while_a_spawned_worker_is_mid_acquisition_is_refused_and_kills_nothing(
    tmp_path, monkeypatch
):
    """Re-review probe A: the worker is released into the acquisition after the NACK."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    acquisition = ControlledAcquisition(lock, monkeypatch)
    acquisition.start()
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        acquisition.allow_take.set()  # the worker's take lands after the refused NACK
        assert acquisition.attempted.wait(5)
        took = acquisition.took
    finally:
        acquisition.stop()

    assert requeued is False
    assert took is True  # the worker owns its device; nothing contested it
    worker.kill.assert_not_called()
    assert state.queue_items[0]["status"] == "starting"
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
async def test_nack_before_any_worker_spawn_requeues_in_place_and_never_fails_the_run(
    tmp_path, monkeypatch
):
    """Re-review probe B: a refused start is requeued, not settled as failed."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    other = DeviceExecutionLock.reserve("probe", "d1", session_id="r2", lock_scope="host:host-a")
    tickets_before = sorted(p.name for p in _ticket_files(ticket) + _ticket_files(other))
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()
    spawn = AsyncMock()

    async def snapshot_for_spawn():
        snapshot_started.set()
        await release_snapshot.wait()
        return SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")

    store = SimpleNamespace(snapshot_for_spawn=snapshot_for_spawn)
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    state.queue_items.extend([row("r1", ticket=ticket), row("r2", ticket=other)])
    state.queue_items[1]["status"] = "pending"
    host_admission.heartbeat("host-a", HostState.ACTIVE, 2)
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.wait_for(snapshot_started.wait(), 5)  # r1 dispatched, no worker yet

    assert await TaskQueueService.requeue_starting("r1") is True

    spawn.assert_not_called()
    assert [(i["session_id"], i["status"]) for i in state.queue_items][0] == ("r1", "pending")
    assert session_repo.get_session_by_id("r1")["status"] == "queued"
    assert sorted(p.name for p in _ticket_files(ticket) + _ticket_files(other)) == tickets_before
    assert host_admission.snapshot("host-a", [])["starting"] <= 1  # r1 released its slot
    assert "r1" not in TaskQueueService._run_tasks_by_session
    assert "r1" not in state.executing_run_keys


@pytest.mark.asyncio
async def test_nack_while_the_worker_process_is_being_spawned_is_refused(tmp_path, monkeypatch):
    """The process may exist before it is registered: unknown state refuses."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    spawning = asyncio.Event()
    finish_spawn = asyncio.Event()
    worker = MagicMock(pid=os.getpid(), returncode=None)

    async def slow_spawn(*_args, **_kwargs):
        spawning.set()
        await finish_spawn.wait()
        return worker

    snapshot = SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")
    store = SimpleNamespace(snapshot_for_spawn=AsyncMock(return_value=snapshot))
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    monkeypatch.setattr("asyncio.create_subprocess_exec", slow_spawn)
    monkeypatch.setattr(TaskQueueService, "_start_output_forwarder", lambda *_args: None)
    exited = asyncio.Event()

    async def wait_for_exit(_worker):
        await exited.wait()

    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", wait_for_exit)
    state.queue_items.append(row("r1", ticket=ticket))
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.wait_for(spawning.wait(), 5)
    assert "r1" not in state.active_runs  # spawned but not registered yet

    assert await TaskQueueService.requeue_starting("r1") is False

    finish_spawn.set()
    await asyncio.sleep(0)
    assert "r1" in state.active_runs
    worker.kill.assert_not_called()
    assert state.queue_items[0]["status"] == "starting"
    assert len(_ticket_files(ticket)) == 1
    exited.set()


# -- third review: the arbiter must never revoke a lease it cannot prove dead --


@pytest.mark.asyncio
async def test_nack_cannot_reap_an_unreadable_live_worker_lock(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    lock.acquire(blocking=False)
    old_time = time.time() - DeviceExecutionLock._MALFORMED_LOCK_GRACE_SECONDS - 1
    os.utime(lock.path, (old_time, old_time))
    lock.path.chmod(0)
    kills_while_locked = []
    worker.kill.side_effect = lambda: kills_while_locked.append(lock._acquired)
    try:
        with pytest.raises(PermissionError):
            lock.path.read_text()
        first_decision = await TaskQueueService.requeue_starting("r1")
        file_kept_after_refusal = lock.path.exists()
        second_decision = await TaskQueueService.requeue_starting("r1")
        print(
            f"UNREADABLE LIVE LOCK: decisions={[first_decision, second_decision]}, "
            f"file_kept_after_refusal={file_kept_after_refusal}, "
            f"kills_while_locked={kills_while_locked}"
        )
        assert kills_while_locked == []
        assert file_kept_after_refusal
        assert first_decision is False
        assert second_decision is False
    finally:
        if lock.path.exists():
            lock.path.chmod(0o600)
        lock.release()


@pytest.mark.asyncio
async def test_nack_preserves_a_worker_paused_before_owner_metadata_write(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    lease_created = threading.Event()
    allow_write = threading.Event()
    acquired = threading.Event()
    allow_return = threading.Event()
    errors = []
    real_write = os.write
    real_take = lock._try_acquire_owner_lock

    def controlled_write(descriptor, payload):
        if lock.token.encode() in payload:
            lease_created.set()
            assert allow_write.wait(5)
        return real_write(descriptor, payload)

    def controlled_take():
        taken = real_take()
        if taken:
            acquired.set()
            assert allow_return.wait(5)
        return taken

    def acquire():
        try:
            lock.acquire(blocking=False)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(os, "write", controlled_write)
    monkeypatch.setattr(lock, "_try_acquire_owner_lock", controlled_take)
    thread = threading.Thread(target=acquire)
    thread.start()
    assert lease_created.wait(5)
    old_time = time.time() - DeviceExecutionLock._MALFORMED_LOCK_GRACE_SECONDS - 1
    os.utime(lock.path, (old_time, old_time))
    kills_while_locked = []
    worker.kill.side_effect = lambda: kills_while_locked.append(lock._acquired)
    try:
        first_decision = await TaskQueueService.requeue_starting("r1")
        file_kept_after_refusal = lock.path.exists()
        allow_write.set()
        assert acquired.wait(5)
        assert lock._acquired
        second_decision = await TaskQueueService.requeue_starting("r1")
        print(
            f"PAUSED METADATA WRITE: decisions={[first_decision, second_decision]}, "
            f"file_kept_after_refusal={file_kept_after_refusal}, "
            f"kills_while_locked={kills_while_locked}"
        )
        assert kills_while_locked == []
        assert file_kept_after_refusal
        assert first_decision is False
        assert second_decision is False
    finally:
        allow_write.set()
        allow_return.set()
        thread.join(5)
        assert not thread.is_alive()
        lock.release()


# -- fourth review: no arbiter lock exists any more, so acquire() has nothing to reap --


@pytest.mark.asyncio
async def test_aged_worker_acquisition_is_never_disturbed_by_a_nack(tmp_path, monkeypatch):
    """Fourth-review probe, adapted: the NACK takes no lock and cannot race acquire()."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    kills_while_locked = []
    worker.kill.side_effect = lambda: kills_while_locked.append(lock._acquired)
    old_time = time.time() - DeviceExecutionLock._MALFORMED_LOCK_GRACE_SECONDS - 1
    thread_errors = []

    def acquire():
        try:
            lock.acquire(timeout=2, poll_interval=0.01)
        except BaseException as error:  # noqa: BLE001 - recorded for the assertions
            thread_errors.append(error)

    thread = threading.Thread(target=acquire)
    thread.start()
    try:
        for _ in range(100):
            if lock.path.exists():
                os.utime(lock.path, (old_time, old_time))
                break
            await asyncio.sleep(0.01)
        requeued = await TaskQueueService.requeue_starting("r1")
        thread.join(5)
    finally:
        thread.join(5)
        assert not thread.is_alive()
        owner_after = DeviceExecutionLock.get_active_owner("d1", "host:host-a")
        lock.release()

    assert requeued is False
    assert thread_errors == []
    assert owner_after is not None and owner_after.session_id == "r1"
    assert kills_while_locked == []
    assert state.queue_items[0]["status"] == "starting"


@pytest.mark.asyncio
async def test_refused_nack_of_a_dead_other_owner_keeps_the_waiting_run(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, asyncio.Event())
    lock = worker_lock(ticket)
    stale_record = json.dumps(
        {
            "pid": 2**22 + 99,
            "process_created_at": 1.0,
            "token": "previous-worker",
            "device_id": "d1",
            "session_id": "previous-run",
            "lock_scope": "host:host-a",
        }
    )
    lock.path.write_text(stale_record)
    queue_dir = device_lock.get_temp_dir("device-locks") / "artemis-global-device.queue"
    tickets_before = sorted(queue_dir.glob(f"*-{ticket}.wait"))
    assert len(tickets_before) == 1

    assert await TaskQueueService.requeue_starting("r1") is False

    worker.kill.assert_not_called()
    assert worker.returncode is None
    assert lock.path.read_text() == stale_record
    assert state.queue_items[0]["status"] == "starting"
    assert "requeue" not in state.queue_items[0]
    assert "r1" in state.active_runs
    assert session_repo.get_session_by_id("r1")["status"] == "queued"
    assert sorted(queue_dir.glob(f"*-{ticket}.wait")) == tickets_before
    print(
        "DEAD OTHER OWNER: refused=True, worker_alive=True, row=starting, session=queued, ticket_kept=True"
    )


# -- fifth review: terminal settlement and the NACK must not both own the outcome --


@pytest.mark.asyncio
async def test_nack_during_pre_spawn_terminal_settlement_never_requeues_a_failed_session(
    tmp_path, monkeypatch
):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    settling = threading.Event()
    allow_settlement = threading.Event()
    settlement_done = threading.Event()
    actual_settle = LifecycleAuthority.settle_worker_exit

    def controlled_settle(authority, session_id, returncode, manual_stop):
        settling.set()
        try:
            assert allow_settlement.wait(5)
            return actual_settle(authority, session_id, returncode, manual_stop)
        finally:
            settlement_done.set()

    store = SimpleNamespace(
        snapshot_for_spawn=AsyncMock(side_effect=RuntimeError("config snapshot failed"))
    )
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    monkeypatch.setattr(LifecycleAuthority, "settle_worker_exit", controlled_settle)
    spawn = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    state.queue_items.append(row("r1", ticket=ticket))
    TaskQueueService._dispatch_pending_tasks()
    assert await asyncio.to_thread(settling.wait, 5)
    try:
        requeued = await TaskQueueService.requeue_starting("r1")
        allow_settlement.set()
        assert await asyncio.to_thread(settlement_done.wait, 5)
        stored_status = session_repo.get_session_by_id("r1")["status"]
        row_status = next(
            (item["status"] for item in state.queue_items if item["session_id"] == "r1"),
            None,
        )
        print(
            f"PRE-SPAWN SETTLEMENT: requeued={requeued}, "
            f"spawn_calls={spawn.call_count}, row={row_status}, session={stored_status}"
        )
        spawn.assert_not_called()
        assert not requeued or stored_status == "queued"
    finally:
        allow_settlement.set()
        assert await asyncio.to_thread(settlement_done.wait, 5)


@pytest.mark.asyncio
async def test_barrier_counts_a_previously_spawned_start_until_it_finishes(tmp_path, monkeypatch):
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    exited = asyncio.Event()
    worker = await spawned_worker(tmp_path, monkeypatch, ticket, exited)
    state.queue_items.append(row("r2", device="d2"))
    lock = worker_lock(ticket)
    try:
        ack = host_admission.request_barrier("host-a")
        assert ack.starting == 1 and not ack.quiescent
        assert await TaskQueueService.requeue_starting("r1") is False
        worker.kill.assert_not_called()
        lock.acquire(blocking=False)
        TaskQueueService._dispatch_pending_tasks()
        assert state.queue_items[0]["status"] == "running"
        assert state.queue_items[1]["status"] == "pending"
        assert not host_admission.request_barrier("host-a").quiescent
        print(
            "POST-SPAWN BARRIER: refused=True, new_reservations=0, "
            "pre_barrier_start_runs=True, quiescent=False"
        )
        lock.release()
        worker.returncode = 0
        exited.set()
        await asyncio.gather(*list(TaskQueueService._run_tasks))
        assert host_admission.request_barrier("host-a").quiescent
    finally:
        lock.release()
        exited.set()


@pytest.mark.asyncio
async def test_nack_that_wins_before_settlement_is_not_overwritten_by_a_later_failure(
    tmp_path, monkeypatch
):
    """Other order: the NACK lands first; the run's later failure must not settle it."""
    host()
    ticket = DeviceExecutionLock.reserve("probe", "d1", session_id="r1", lock_scope="host:host-a")
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    snapshot_started = asyncio.Event()
    fail_snapshot = asyncio.Event()

    async def snapshot_for_spawn():
        snapshot_started.set()
        await fail_snapshot.wait()
        raise RuntimeError("config snapshot failed")

    store = SimpleNamespace(snapshot_for_spawn=snapshot_for_spawn)
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    settle = MagicMock(wraps=LifecycleAuthority.settle_worker_exit)
    monkeypatch.setattr(LifecycleAuthority, "settle_worker_exit", settle)
    spawn = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    state.queue_items.append(row("r1", ticket=ticket))
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.wait_for(snapshot_started.wait(), 5)
    run_task = TaskQueueService._run_tasks_by_session["r1"]

    assert await TaskQueueService.requeue_starting("r1") is True
    fail_snapshot.set()  # too late: the run was cancelled before it could fail
    await asyncio.gather(run_task, return_exceptions=True)
    await asyncio.sleep(0.05)

    spawn.assert_not_called()
    settle.assert_not_called()
    assert session_repo.get_session_by_id("r1")["status"] == "queued"
    assert [(i["session_id"], i["status"]) for i in state.queue_items] == [("r1", "pending")]


@pytest.mark.asyncio
async def test_settlement_never_overwrites_a_requeued_row(monkeypatch):
    assert session_repo.create_queued_session("r1", "probe", "flash", "d1", 1.0, None)
    state.queue_items.append({**row("r1"), "requeue": True})

    status = await TaskQueueService._persist_terminal_session_status("r1", 1, False)

    assert status == "queued"
    assert session_repo.get_session_by_id("r1")["status"] == "queued"
    assert "settling" not in state.queue_items[0]


@pytest.mark.asyncio
async def test_a_settling_start_is_refused_by_a_nack_even_before_any_thread_runs(monkeypatch):
    host()
    state.queue_items.append({**row("r1", ticket="t1"), "status": "starting", "settling": True})

    assert await TaskQueueService.requeue_starting("r1") is False
    assert state.queue_items[0]["status"] == "starting"
