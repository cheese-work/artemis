"""A queued host snapshot with the host agent flag off fails that task, not the scheduler (CHE-1094)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.admin_console.core.state import state
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import DeviceExecutionLock

HOST_SNAPSHOT = {
    "host": "127.0.0.1",
    "port": 40000,
    "identity": "host:lab-1",
    "mode": "host",
    "host_id": "lab-1",
    "generation": 1,
}


@pytest.fixture(autouse=True)
def flag_off_and_clean_state(monkeypatch):
    monkeypatch.delenv("ARTEMIS_HOST_AGENT", raising=False)
    monkeypatch.setattr(DeviceExecutionLock, "cancel_reservation", MagicMock())
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.draining = False
    yield
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()


def _item(session_id: str, endpoint: dict | None, status: str = "pending") -> dict:
    item = {
        "session_id": session_id,
        "goal": "open settings",
        "device_serial": "emulator-5554",
        "status": status,
        "ingress": "frontend",
    }
    if endpoint is not None:
        item["adb_endpoint"] = endpoint
    return item


@pytest.mark.asyncio
async def test_a_pending_host_snapshot_does_not_break_the_scheduler_pass():
    host_item = _item("host-task", HOST_SNAPSHOT)
    state.queue_items.append(host_item)
    started: list[str] = []

    async def fake_execute(task_item):
        started.append(task_item["session_id"])

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()  # must not raise
        await asyncio.sleep(0)

    # The task is handed to its own run, where launch fails it with a clear error.
    assert started == ["host-task"]


@pytest.mark.asyncio
async def test_an_in_flight_host_snapshot_does_not_break_the_scheduler_pass():
    state.queue_items.append(_item("running-host", HOST_SNAPSHOT, status="running"))
    state.queue_items.append(_item("local-task", None))
    started: list[str] = []

    async def fake_execute(task_item):
        started.append(task_item["session_id"])

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()  # must not raise
        await asyncio.sleep(0)

    assert "local-task" in started


@pytest.mark.asyncio
async def test_launching_a_host_snapshot_with_the_flag_off_fails_that_task_clearly(
    monkeypatch, caplog
):
    spawned, persisted = [], []

    async def no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a worker must not start")

    async def record(sess_id, returncode, manual_stop):
        persisted.append((sess_id, returncode))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    monkeypatch.setattr(TaskQueueService, "_begin_task_run", classmethod(lambda cls, *a: None))
    monkeypatch.setattr(
        TaskQueueService,
        "_persist_terminal_session_status",
        classmethod(lambda cls, *a, **k: record(*a, **k)),
    )
    monkeypatch.setattr(TaskQueueService, "_deliver_outcome", classmethod(lambda cls, *a: None))

    await TaskQueueService._execute_task_item(_item("host-task", HOST_SNAPSHOT))

    assert spawned == [] and persisted == [("host-task", 1)]
    assert "host agent" in caplog.text.lower() and "disabled" in caplog.text.lower()
