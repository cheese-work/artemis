"""Host admission and the maintenance barrier at the server seam (CHE-1128).

Everything runs against the in-memory queue and the real dispatcher with fake
runs: no ADB, adb server, browser, USB or device.
"""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.server import app
from apps.admin_console.services.host_admission import (
    HostState,
    RunPhase,
    WaitReason,
    host_admission,
)
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import DeviceExecutionLock, device_lock, trace_store

HOST_A = "host-a"
HOST_B = "host-b"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    lock_dir = tmp_path / "device-locks"
    lock_dir.mkdir()
    monkeypatch.setattr(device_lock, "get_temp_dir", lambda _sub=None: lock_dir)
    monkeypatch.setattr(session_repo, "db_path", tmp_path / "sessions.db")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        TaskQueueService, "_reject_unavailable_device", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(TaskQueueService, "_concurrency_limit", classmethod(lambda cls: 0))
    host_admission.reset()
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.cancelled_session_ids.clear()
    state.draining = False
    TaskQueueService._run_tasks.clear()
    TaskQueueService._run_tasks_by_session.clear()
    yield
    for task in list(TaskQueueService._run_tasks):
        task.cancel()
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    host_admission.reset()


class Runs:
    """Fake runs that stay in flight until released."""

    def __init__(self, monkeypatch):
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        monkeypatch.setattr(TaskQueueService, "_execute_task_item", self._execute)

    async def _execute(self, task_item):
        sid = task_item["session_id"]
        self.started.append(sid)
        self.gates[sid] = asyncio.Event()
        try:
            await self.gates[sid].wait()
        finally:
            if not task_item.get("requeue"):
                TaskQueueService._remove_task(sid)
            host_admission.release(sid)

    async def tick(self):
        TaskQueueService._dispatch_pending_tasks()
        await asyncio.sleep(0)

    async def finish(self, sid):
        self.gates[sid].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.fixture
def runs(monkeypatch):
    return Runs(monkeypatch)


def _host_agent(host_id=HOST_A, devices=("d1", "d2", "d3"), state_=HostState.ACTIVE, max_runs=None):
    host_admission.heartbeat(host_id, state_, max_runs)
    for device in devices:
        host_admission.share_device(host_id, device)


def _row(sid, device="d1", host=HOST_A, ticket=None):
    return {
        "session_id": sid,
        "goal": sid,
        "profile": "flash",
        "status": "pending",
        "device_serial": device,
        "host_id": host,
        "ingress": "frontend",
        "queue_ticket": ticket,
    }


def _statuses():
    return {i["session_id"]: i["status"] for i in state.queue_items}


# -- state, capacity and device claims (module) ----------------------------


def test_unknown_host_is_offline_until_its_agent_reports():
    assert host_admission.try_reserve(HOST_A, "s", "d1") is WaitReason.HOST_OFFLINE
    _host_agent()
    assert host_admission.try_reserve(HOST_A, "s", "d1") is None


@pytest.mark.parametrize(
    ("barrier", "reason"),
    [
        (HostState.DRAINING, WaitReason.HOST_DRAINING),
        (HostState.MAINTENANCE, WaitReason.HOST_MAINTENANCE),
        (HostState.UPDATE_REQUIRED, WaitReason.HOST_UPDATE_REQUIRED),
    ],
)
def test_any_non_active_state_refuses_new_starts(barrier, reason):
    _host_agent(state_=barrier)
    assert host_admission.try_reserve(HOST_A, "s", "d1") is reason


def test_barrier_ack_counts_starting_running_and_cleaning_up_apart():
    _host_agent()
    for sid in ("starting", "running", "cleaning"):
        assert host_admission.try_reserve(HOST_A, sid, "d1") is None
    host_admission.mark("running", RunPhase.RUNNING)
    host_admission.mark("cleaning", RunPhase.CLEANING_UP)

    ack = host_admission.request_barrier(HOST_A)

    assert (ack.starting, ack.running, ack.cleaning_up) == (1, 1, 1)
    assert ack.state is HostState.MAINTENANCE
    assert not ack.quiescent
    assert host_admission.try_reserve(HOST_A, "late", "d1") is WaitReason.HOST_MAINTENANCE
    for sid in ("starting", "running", "cleaning"):
        host_admission.release(sid)
    assert host_admission.request_barrier(HOST_A).quiescent


def test_a_non_barrier_state_cannot_be_requested():
    with pytest.raises(ValueError, match="not a barrier"):
        host_admission.request_barrier(HOST_A, HostState.ACTIVE)


def test_cancel_update_clears_the_barrier_but_not_offline():
    _host_agent()
    host_admission.request_barrier(HOST_A, HostState.UPDATE_REQUIRED)
    host_admission.cancel_update(HOST_A)
    assert host_admission.state_of(HOST_A) is HostState.ACTIVE

    host_admission.disconnect(HOST_A)
    host_admission.cancel_update(HOST_A)
    assert host_admission.state_of(HOST_A) is HostState.OFFLINE


def test_disconnect_during_drain_is_offline_and_agent_rereports_on_reconnect():
    _host_agent()
    host_admission.request_barrier(HOST_A, HostState.DRAINING)
    assert host_admission.try_reserve(HOST_A, "run", "d1") is WaitReason.HOST_DRAINING

    host_admission.disconnect(HOST_A)
    assert host_admission.try_reserve(HOST_A, "run", "d1") is WaitReason.HOST_OFFLINE

    host_admission.heartbeat(HOST_A, HostState.DRAINING)  # the agent re-reports its barrier
    assert host_admission.try_reserve(HOST_A, "run", "d1") is WaitReason.HOST_DRAINING


def test_capacity_is_per_host_and_freed_at_release():
    _host_agent(max_runs=2)
    _host_agent(HOST_B, devices=("e1",), max_runs=1)
    assert host_admission.try_reserve(HOST_A, "a1", "d1") is None
    assert host_admission.try_reserve(HOST_A, "a2", "d2") is None
    assert host_admission.try_reserve(HOST_A, "a3", "d3") is WaitReason.HOST_FULL
    assert host_admission.try_reserve(HOST_B, "b1", "e1") is None  # another host is unaffected

    host_admission.release("a1")
    assert host_admission.try_reserve(HOST_A, "a3", "d3") is None


def test_a_retried_reservation_never_double_counts():
    _host_agent(max_runs=1)
    assert host_admission.try_reserve(HOST_A, "a1", "d1") is None
    assert host_admission.try_reserve(HOST_A, "a1", "d1") is None
    assert host_admission.snapshot(HOST_A, [])["starting"] == 1


@pytest.mark.parametrize("ingress", ["web", "cli", "sdk"])
def test_last_slot_goes_to_exactly_one_concurrent_reservation(ingress):
    _host_agent(max_runs=3)
    host_admission.try_reserve(HOST_A, "held-1", "d1")
    host_admission.try_reserve(HOST_A, "held-2", "d1")
    barrier = threading.Barrier(12)
    results: list[object] = []

    def reserve(index):
        barrier.wait()
        results.append(host_admission.try_reserve(HOST_A, f"{ingress}-{index}", "d1"))

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(None) == 1
    assert results.count(WaitReason.HOST_FULL) == 11
    assert host_admission.snapshot(HOST_A, [])["starting"] == 3


def test_unshared_device_stops_new_starts_but_not_a_reserved_run():
    _host_agent()
    assert host_admission.try_reserve(HOST_A, "run", "d1") is None

    host_admission.unshare_device(HOST_A, "d1")

    assert host_admission.try_reserve(HOST_A, "next", "d1") is WaitReason.DEVICE_NOT_SHARED
    assert host_admission.snapshot(HOST_A, [])["starting"] == 1


def test_one_device_id_on_two_computers_dispatches_to_neither_until_an_admin_chooses():
    _host_agent(devices=("phone",))
    _host_agent(HOST_B, devices=("phone",))

    assert host_admission.needs_attention("phone") == [HOST_A, HOST_B]
    for host in (HOST_A, HOST_B):
        assert host_admission.try_reserve(host, f"s-{host}", "phone") is (
            WaitReason.DEVICE_NEEDS_ATTENTION
        )

    host_admission.use_this_computer("phone", HOST_B)
    assert host_admission.needs_attention("phone") == []
    assert host_admission.try_reserve(HOST_A, "s-a", "phone") is WaitReason.DEVICE_NOT_SHARED
    assert host_admission.try_reserve(HOST_B, "s-b", "phone") is None


def test_declaring_different_phones_lets_both_computers_dispatch():
    _host_agent(devices=("phone",))
    _host_agent(HOST_B, devices=("phone",))

    host_admission.different_phones("phone")

    assert host_admission.needs_attention("phone") == []
    assert host_admission.try_reserve(HOST_A, "s-a", "phone") is None
    assert host_admission.try_reserve(HOST_B, "s-b", "phone") is None


def test_snapshot_counts_waiting_rows_apart_from_runs():
    _host_agent()
    host_admission.try_reserve(HOST_A, "live", "d1")
    rows = [_row("w1"), _row("w2"), _row("other", host=HOST_B), {"status": "running"}]

    snapshot = host_admission.snapshot(HOST_A, rows)

    assert (snapshot["waiting"], snapshot["starting"], snapshot["running"]) == (2, 1, 0)


def test_host_barrier_never_touches_the_server_wide_deploy_drain():
    _host_agent()
    state.queue_items.append(_row("w1"))
    before = TaskQueueService.active_run_count()

    host_admission.request_barrier(HOST_A)

    assert state.draining is False
    assert TaskQueueService.active_run_count() == before


# -- the dispatcher hook ----------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_marks_a_row_starting_and_holds_the_slot(runs):
    _host_agent()
    state.queue_items.append(_row("r1"))

    await runs.tick()

    assert _statuses() == {"r1": "starting"}
    assert host_admission.snapshot(HOST_A, state.queue_items)["starting"] == 1


@pytest.mark.asyncio
async def test_row_becomes_running_with_execution_started_at_when_the_lock_is_held(
    runs, monkeypatch
):
    _host_agent()
    assert session_repo.create_queued_session("r1", "g", "flash", "d1", 100.0, None)
    state.queue_items.append(_row("r1"))
    await runs.tick()
    assert "execution_started_at" not in state.queue_items[0]

    monkeypatch.setattr(TaskQueueService, "_held_lock_session_ids", classmethod(lambda cls: {"r1"}))
    await runs.tick()

    item = state.queue_items[0]
    assert item["status"] == "running"
    assert item["execution_started_at"] >= 100.0
    assert host_admission.snapshot(HOST_A, state.queue_items)["running"] == 1
    row = session_repo.get_session_by_id("r1")
    assert row["execution_started_at"] == item["execution_started_at"]
    assert row["start_time"] == 100.0  # the enqueue time is a separate fact


def test_execution_started_at_is_written_once():
    assert session_repo.create_queued_session("r1", "g", "flash", "d1", 1.0, None)

    assert session_repo.lifecycle.mark_execution_started("r1", 5.0) is True
    assert session_repo.lifecycle.mark_execution_started("r1", 9.0) is False

    with sqlite3.connect(session_repo.db_path) as conn:
        assert conn.execute("SELECT execution_started_at FROM sessions").fetchone() == (5.0,)


@pytest.mark.asyncio
async def test_a_barred_host_keeps_its_rows_waiting_in_place_while_other_hosts_run(runs):
    _host_agent(state_=HostState.MAINTENANCE)
    _host_agent(HOST_B, devices=("e1",))
    state.queue_items.extend([_row("a1"), _row("b1", "e1", HOST_B), _row("a2", "d2")])

    await runs.tick()

    assert runs.started == ["b1"]
    assert [i["session_id"] for i in state.queue_items] == ["a1", "b1", "a2"]
    held = state.queue_items[0]
    assert (held["status"], held["wait_reason"]) == ("pending", "host_maintenance")


@pytest.mark.asyncio
async def test_a_full_host_makes_later_rows_wait_for_a_slot(runs):
    _host_agent(max_runs=1)
    state.queue_items.extend([_row("a1"), _row("a2", "d2")])

    await runs.tick()
    assert runs.started == ["a1"]
    assert state.queue_items[1]["wait_reason"] == "host_full"

    await runs.finish("a1")
    await runs.tick()
    assert runs.started == ["a1", "a2"]
    assert "wait_reason" not in state.queue_items[0] or state.queue_items[0]["status"] != "pending"


@pytest.mark.asyncio
async def test_row_waiting_behind_a_busy_device_holds_no_capacity(runs):
    _host_agent(max_runs=2)
    state.queue_items.extend([_row("a1"), _row("a2"), _row("a3", "d2")])

    await runs.tick()

    assert runs.started == ["a1", "a3"]  # a2 never reserved a slot while d1 was busy
    assert host_admission.snapshot(HOST_A, state.queue_items)["starting"] == 2


@pytest.mark.asyncio
async def test_barrier_during_active_and_queued_runs_keeps_order_on_release(runs):
    """Acceptance: active runs finish, new starts are blocked, order survives release."""
    _host_agent()
    state.queue_items.append(_row("active"))
    await runs.tick()
    state.queue_items.extend([_row("q1", "d2"), _row("q2", "d3"), _row("q3", "d2")])

    ack = host_admission.request_barrier(HOST_A)
    await runs.tick()

    assert ack.starting == 1
    assert runs.started == ["active"]  # q1..q3 blocked
    assert _statuses()["active"] == "starting"  # the active run is untouched

    await runs.finish("active")
    await runs.tick()
    assert runs.started == ["active"]  # still blocked after the active run ended

    host_admission.cancel_update(HOST_A)
    await runs.tick()
    assert runs.started == ["active", "q1", "q2"]  # original order; q3 waits on d2 behind q1
    await runs.finish("q1")
    await runs.tick()
    assert runs.started == ["active", "q1", "q2", "q3"]


@pytest.mark.asyncio
async def test_cancelling_a_queued_run_under_the_barrier_keeps_the_rest_in_order(runs):
    _host_agent()
    state.queue_items.extend([_row("q1", "d1"), _row("q2", "d2"), _row("q3", "d3")])
    host_admission.request_barrier(HOST_A)
    await runs.tick()

    assert TaskQueueService.cancel_queued("q2") == "cancelled"
    host_admission.cancel_update(HOST_A)
    await runs.tick()

    assert runs.started == ["q1", "q3"]


@pytest.mark.asyncio
async def test_server_restart_mid_drain_leaves_hosts_offline_until_they_report(runs):
    _host_agent()
    host_admission.request_barrier(HOST_A, HostState.DRAINING)
    state.queue_items.append(_row("q1"))

    host_admission.reset()  # the server restarted: only the agent knows the host state
    await runs.tick()
    assert runs.started == []
    assert state.queue_items[0]["wait_reason"] == "host_offline"

    _host_agent()
    await runs.tick()
    assert runs.started == ["q1"]


@pytest.mark.asyncio
async def test_dispatch_vs_unshare_leaves_the_row_waiting(runs):
    _host_agent()
    state.queue_items.append(_row("q1"))
    host_admission.unshare_device(HOST_A, "d1")

    await runs.tick()

    assert runs.started == []
    assert state.queue_items[0]["wait_reason"] == "device_not_shared"


@pytest.mark.asyncio
async def test_a_run_cancelled_before_it_starts_releases_its_slot(runs):
    _host_agent(max_runs=1)
    state.queue_items.append(_row("a1"))
    await runs.tick()
    (run_task,) = TaskQueueService._run_tasks
    run_task.cancel()
    await asyncio.gather(run_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert host_admission.snapshot(HOST_A, [])["starting"] == 0


@pytest.mark.asyncio
async def test_flag_off_keeps_the_legacy_running_transition(runs, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "0")
    local = {k: v for k, v in _row("l1").items() if k != "host_id"}
    state.queue_items.append(local)

    await runs.tick()

    assert _statuses() == {"l1": "running"}


# -- lock identity ----------------------------------------------------------


def test_lock_key_is_host_id_plus_opaque_device_id():
    one = TaskQueueService._task_target(_row("s", "sd-abc", HOST_A))
    two = TaskQueueService._task_target(_row("s", "sd-abc", HOST_B))
    local = TaskQueueService._task_target({"session_id": "s", "device_serial": "sd-abc"})

    assert one.lock_key == "host:host-a/sd-abc"
    assert one.lock_key != two.lock_key  # same id on two computers never shares a lock
    assert local.lock_key.startswith("tcp:")  # local devices keep the endpoint identity


@pytest.mark.asyncio
async def test_same_phone_id_on_two_computers_runs_in_parallel_once_declared_different(runs):
    _host_agent(devices=("sd-x",))
    _host_agent(HOST_B, devices=("sd-x",))
    host_admission.different_phones("sd-x")
    state.queue_items.extend([_row("a1", "sd-x"), _row("b1", "sd-x", HOST_B), _row("a2", "sd-x")])

    await runs.tick()

    assert runs.started == ["a1", "b1"]  # a2 waits behind a1 on host-a only


# -- enqueue ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_run_needs_the_flag_and_a_device_ref(monkeypatch):
    with pytest.raises(ValueError, match="device"):
        await TaskQueueService.enqueue_tasks(["g"], host_id=HOST_A)
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "0")
    with pytest.raises(ValueError, match="ARTEMIS_HOST_AGENT"):
        await TaskQueueService.enqueue_tasks(["g"], host_id=HOST_A, device_serial="d1")
    assert state.queue_items == []


@pytest.mark.asyncio
async def test_host_run_is_queued_under_its_host_lock_scope_even_when_the_host_is_full():
    _host_agent(max_runs=0)

    result = await TaskQueueService.enqueue_tasks(
        ["g"], host_id=HOST_A, device_serial="sd-abc", ingress="sdk"
    )

    (item,) = result["tasks"]
    assert item["host_id"] == HOST_A and item["status"] == "pending"
    (ticket,) = [q for q in DeviceExecutionLock.get_queued_tasks() if q["session_id"]]
    assert ticket["device_id"] == "sd-abc"
    TaskQueueService._reject_unavailable_device.assert_not_called()  # no local adb involved


# -- NACK and requeue -------------------------------------------------------


def _ticket_names():
    queue_dir = device_lock.get_temp_dir("device-locks") / "artemis-global-device.queue"
    return sorted(Path(p).name for p in queue_dir.glob("*.wait"))


@pytest.mark.asyncio
async def test_a_nacked_start_requeues_at_its_original_position_and_ticket(runs, monkeypatch):
    _host_agent()
    tickets = [
        DeviceExecutionLock.reserve("t", f"d{i}", session_id=f"r{i}", lock_scope=f"host:{HOST_A}")
        for i in (1, 2, 3)
    ]
    state.queue_items.extend(_row(f"r{i}", f"d{i}", ticket=tickets[i - 1]) for i in (1, 2, 3))
    names_before = _ticket_names()
    await runs.tick()
    assert runs.started == ["r1", "r2", "r3"]

    host_admission.request_barrier(HOST_A)  # the agent raises the barrier, then NACKs r2
    assert await TaskQueueService.requeue_starting("r2") is True

    assert [i["session_id"] for i in state.queue_items] == ["r1", "r2", "r3"]
    assert _statuses() == {"r1": "starting", "r2": "pending", "r3": "starting"}
    assert _ticket_names() == names_before  # same ticket files, same timestamps
    assert host_admission.snapshot(HOST_A, state.queue_items)["starting"] == 2

    await runs.tick()
    assert runs.started == ["r1", "r2", "r3"]  # barrier still up: no restart

    host_admission.cancel_update(HOST_A)
    await runs.tick()
    assert runs.started == ["r1", "r2", "r3", "r2"]


@pytest.mark.asyncio
async def test_nack_after_the_worker_spawned_kills_it_and_hands_the_ticket_back(monkeypatch):
    """The real run path: the spawned worker owned the ticket and dies with the NACK."""
    _host_agent()
    ticket = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    state.queue_items.append(_row("r1", ticket=ticket))
    ticket_file = next(
        (device_lock.get_temp_dir("device-locks") / "artemis-global-device.queue").glob("*.wait")
    )
    proc = MagicMock(pid=2**22 + 12345, returncode=None)
    hang = asyncio.Event()

    async def spawn(*_args, **_kwargs):
        return proc

    async def wait_forever(_proc):
        await hang.wait()

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", wait_forever)
    monkeypatch.setattr(TaskQueueService, "_start_output_forwarder", lambda *_a: None)
    monkeypatch.setattr(TaskQueueService, "_held_lock_session_ids", classmethod(lambda cls: set()))
    TaskQueueService._dispatch_pending_tasks()
    for _ in range(50):
        if "r1" in state.active_runs:
            break
        await asyncio.sleep(0.01)
    assert json.loads(ticket_file.read_text())["pid"] == proc.pid  # the worker owns it now

    assert await TaskQueueService.requeue_starting("r1") is True

    proc.kill.assert_called_once()
    assert [i["status"] for i in state.queue_items] == ["pending"]
    assert "pid" not in state.queue_items[0]
    assert "r1" not in state.active_runs
    assert json.loads(ticket_file.read_text())["pid"] == os.getpid()  # same file, alive owner
    assert host_admission.snapshot(HOST_A, [])["starting"] == 0


@pytest.mark.asyncio
async def test_requeue_is_refused_once_the_worker_holds_its_lock(runs, monkeypatch):
    _host_agent()
    state.queue_items.append(_row("r1"))
    await runs.tick()
    monkeypatch.setattr(TaskQueueService, "_held_lock_session_ids", classmethod(lambda cls: {"r1"}))

    assert await TaskQueueService.requeue_starting("r1") is False
    assert _statuses() == {"r1": "starting"}


@pytest.mark.asyncio
async def test_requeue_ignores_rows_that_are_not_starting(runs):
    _host_agent()
    state.queue_items.append(_row("w1"))

    assert await TaskQueueService.requeue_starting("w1") is False
    assert await TaskQueueService.requeue_starting("missing") is False


# -- cancel-queued ----------------------------------------------------------


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost")


@pytest.mark.asyncio
async def test_cancel_queued_cancels_a_waiting_run():
    assert session_repo.create_queued_session("q1", "g", "flash", "d1", None, None)
    state.queue_items.extend([_row("q1"), _row("q2", "d2")])

    async with _client() as client:
        response = await client.post("/api/tasks/q1/cancel-queued")

    assert response.status_code == 200
    assert response.json() == {"status": "cancelled", "session_id": "q1"}
    assert [i["session_id"] for i in state.queue_items] == ["q2"]
    assert session_repo.get_session_by_id("q1")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_queued_never_stops_a_started_run(runs, monkeypatch):
    stop = MagicMock()
    monkeypatch.setattr(TaskQueueService, "stop_tasks", stop)
    _host_agent()
    assert session_repo.create_queued_session("r1", "g", "flash", "d1", None, None)
    state.queue_items.append(_row("r1"))
    await runs.tick()  # the click raced the dispatch: the run is already starting

    async with _client() as client:
        starting = await client.post("/api/tasks/r1/cancel-queued")
        monkeypatch.setattr(
            TaskQueueService, "_held_lock_session_ids", classmethod(lambda cls: {"r1"})
        )
        await runs.tick()
        running = await client.post("/api/tasks/r1/cancel-queued")

    assert starting.json() == {"status": "already_started", "session_id": "r1"}
    assert running.json() == {"status": "already_started", "session_id": "r1"}
    stop.assert_not_called()
    assert _statuses() == {"r1": "running"}
    assert session_repo.get_session_by_id("r1")["status"] == "queued"  # untouched


@pytest.mark.asyncio
async def test_cancel_queued_answers_already_started_for_a_finished_run_and_404_for_unknown():
    assert session_repo.create_queued_session("done", "g", "flash", "d1", None, None)
    session_repo.update_session_status("done", "completed", 1.0)

    async with _client() as client:
        finished = await client.post("/api/tasks/done/cancel-queued")
        unknown = await client.post("/api/tasks/nope/cancel-queued")

    assert finished.json()["status"] == "already_started"
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_cancel_queued_cancels_a_persisted_queued_row_whose_queue_was_lost():
    assert session_repo.create_queued_session("orphan", "g", "flash", "d1", None, None)

    async with _client() as client:
        response = await client.post("/api/tasks/orphan/cancel-queued")

    assert response.json()["status"] == "cancelled"
    assert session_repo.get_session_by_id("orphan")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_api_stop_stays_the_only_way_to_stop_a_running_run(runs):
    """Queued runs have their own route; /api/stop keeps its unconditional contract."""
    state.queue_items.append(_row("q1"))

    async with _client() as client:
        response = await client.post("/api/stop", json={"session_id": "q1"})

    assert response.json() == {"status": "stopped", "session_id": "q1"}
    assert state.queue_items == []


# -- review round 1: NACK arbitration, external lock owners, host-scoped debounce --


async def _spawned_worker(tmp_path, monkeypatch, ticket):
    """Dispatch r1 through the real run path with a fake worker process."""
    worker = MagicMock(pid=os.getpid(), returncode=None)
    snapshot = SimpleNamespace(environment={}, config_path=tmp_path / "snapshot.json")
    store = SimpleNamespace(snapshot_for_spawn=AsyncMock(return_value=snapshot))
    monkeypatch.setattr("apps.admin_console.services.config_store.get_config_store", lambda: store)
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=worker))
    monkeypatch.setattr(TaskQueueService, "_start_output_forwarder", lambda *_a: None)
    hanging = asyncio.Event()

    async def wait_for_exit(_worker):
        await hanging.wait()

    monkeypatch.setattr(TaskQueueService, "_wait_for_worker_process", wait_for_exit)
    state.queue_items.append(_row("r1", ticket=ticket))
    TaskQueueService._dispatch_pending_tasks()
    await asyncio.sleep(0)
    assert "r1" in state.active_runs
    return worker


def _worker_lock(ticket):
    return DeviceExecutionLock(
        description="worker",
        device_id="d1",
        session_id="r1",
        queue_ticket=ticket,
        lock_scope=f"host:{HOST_A}",
        concurrency_mode="per_device",
    )


def _ticket_files(ticket):
    queue_dir = device_lock.get_temp_dir("device-locks") / "artemis-global-device.queue"
    return list(queue_dir.glob(f"*-{ticket}.wait"))


@pytest.mark.asyncio
async def test_nack_never_kills_a_worker_that_acquires_the_lock_during_the_nack(
    tmp_path, monkeypatch
):
    """The worker may win the lock at any point inside the NACK; the kill must not hit it."""
    _host_agent()
    ticket = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    worker = await _spawned_worker(tmp_path, monkeypatch, ticket)
    lock = _worker_lock(ticket)
    killed_while_locked: list[bool] = []
    worker.kill.side_effect = lambda: killed_while_locked.append(lock._acquired)
    real_snapshot = TaskQueueService._held_lock_session_ids.__func__

    def snapshot_then_worker_acquires(cls):
        observed = real_snapshot(cls)
        try:
            lock.acquire(blocking=False)  # the worker's acquisition races the NACK
        except device_lock.DeviceBusyError:
            pass  # the NACK already fenced it: it aborted without the lock
        return observed

    monkeypatch.setattr(
        TaskQueueService,
        "_held_lock_session_ids",
        classmethod(snapshot_then_worker_acquires),
    )
    try:
        await TaskQueueService.requeue_starting("r1")
    finally:
        lock.release()

    assert True not in killed_while_locked


@pytest.mark.asyncio
async def test_nack_is_refused_when_the_worker_already_holds_the_lock(tmp_path, monkeypatch):
    _host_agent()
    ticket = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    worker = await _spawned_worker(tmp_path, monkeypatch, ticket)
    lock = _worker_lock(ticket)
    lock.acquire(blocking=False)  # lock held; the dispatcher has not promoted the row yet
    try:
        assert await TaskQueueService.requeue_starting("r1") is False
    finally:
        lock.release()

    worker.kill.assert_not_called()
    assert _statuses() == {"r1": "starting"}


@pytest.mark.asyncio
async def test_nack_fails_closed_and_keeps_the_ticket_when_lock_state_is_unreadable(
    tmp_path, monkeypatch
):
    _host_agent()
    ticket = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    worker = await _spawned_worker(tmp_path, monkeypatch, ticket)
    monkeypatch.setattr(
        DeviceExecutionLock, "get_active_owners", MagicMock(side_effect=OSError("unreadable"))
    )

    assert await TaskQueueService.requeue_starting("r1") is False

    worker.kill.assert_not_called()
    assert _statuses() == {"r1": "starting"}
    assert len(_ticket_files(ticket)) == 1  # original ticket, original timestamp
    monkeypatch.undo()


@pytest.mark.asyncio
async def test_nack_of_a_lock_holding_worker_with_unreadable_state_keeps_it_running(
    tmp_path, monkeypatch
):
    _host_agent()
    ticket = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    worker = await _spawned_worker(tmp_path, monkeypatch, ticket)
    lock = _worker_lock(ticket)
    lock.acquire(blocking=False)
    monkeypatch.setattr(
        DeviceExecutionLock, "get_active_owners", MagicMock(side_effect=OSError("unreadable"))
    )
    try:
        assert await TaskQueueService.requeue_starting("r1") is False
    finally:
        lock.release()

    worker.kill.assert_not_called()


@pytest.mark.asyncio
async def test_busy_device_owned_outside_server_state_does_not_take_the_last_host_slot(monkeypatch):
    _host_agent(max_runs=2)
    assert host_admission.try_reserve(HOST_A, "external", "d1") is None
    host_admission.mark("external", RunPhase.RUNNING)
    external = DeviceExecutionLock(
        description="other ingress",
        device_id="d1",
        session_id="external",
        lock_scope=f"host:{HOST_A}",
        concurrency_mode="per_device",
    )
    external.acquire(blocking=False)
    started: list[str] = []
    hanging = asyncio.Event()

    async def fake_execute(item):
        started.append(item["session_id"])
        await hanging.wait()

    monkeypatch.setattr(TaskQueueService, "_execute_task_item", fake_execute)
    state.queue_items.extend([_row("busy-d1", "d1"), _row("idle-d2", "d2")])
    try:
        TaskQueueService._dispatch_pending_tasks()
        await asyncio.sleep(0)
    finally:
        external.release()
        hanging.set()

    assert started == ["idle-d2"]
    assert state.queue_items[0]["wait_reason"] == "device_busy"
    assert host_admission.snapshot(HOST_A, [])["starting"] == 1  # d1 reserved nothing


@pytest.mark.asyncio
async def test_row_behind_another_ingress_ticket_holds_no_slot_until_it_is_head(runs):
    _host_agent(max_runs=2)
    DeviceExecutionLock.reserve("other", "d1", session_id="sdk-1", lock_scope=f"host:{HOST_A}")
    own = DeviceExecutionLock.reserve("t", "d1", session_id="r1", lock_scope=f"host:{HOST_A}")
    state.queue_items.extend([_row("r1", "d1", ticket=own), _row("r2", "d2")])

    await runs.tick()

    assert runs.started == ["r2"]  # r1 waits behind the other ingress; it took no slot
    assert state.queue_items[0]["wait_reason"] == "device_busy"
    assert host_admission.snapshot(HOST_A, [])["starting"] == 1


@pytest.mark.asyncio
async def test_unreadable_lock_state_keeps_host_rows_waiting(runs, monkeypatch):
    _host_agent()
    state.queue_items.append(_row("r1"))
    monkeypatch.setattr(
        DeviceExecutionLock, "get_active_owner", MagicMock(side_effect=OSError("unreadable"))
    )

    await runs.tick()

    assert runs.started == []
    assert host_admission.snapshot(HOST_A, [])["starting"] == 0


@pytest.mark.asyncio
async def test_distinct_hosts_do_not_debounce_each_others_submissions():
    _host_agent(devices=("d1",))
    _host_agent(HOST_B, devices=("d1",))
    host_admission.different_phones("d1")

    first = await TaskQueueService.enqueue_tasks(["same"], host_id=HOST_A, device_serial="d1")
    second = await TaskQueueService.enqueue_tasks(["same"], host_id=HOST_B, device_serial="d1")
    again = await TaskQueueService.enqueue_tasks(["same"], host_id=HOST_B, device_serial="d1")

    assert (first["enqueued_count"], second["enqueued_count"]) == (1, 1)
    assert second["tasks"][0]["host_id"] == HOST_B
    assert again["enqueued_count"] == 0  # the same computer's double-click is still debounced


@pytest.mark.asyncio
async def test_a_local_submission_is_not_debounced_into_a_host_run(monkeypatch):
    _host_agent(devices=("d1",))
    monkeypatch.setattr(
        "artemis.runtime.device_pool.device_pool.select_device_async",
        AsyncMock(return_value="d1"),
    )
    host_run = await TaskQueueService.enqueue_tasks(["same"], host_id=HOST_A, device_serial="d1")
    local = await TaskQueueService.enqueue_tasks(["same"], device_serial="d1")

    assert host_run["enqueued_count"] == local["enqueued_count"] == 1
