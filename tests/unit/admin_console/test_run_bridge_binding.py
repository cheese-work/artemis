"""A run bound to a browser phone keeps its bridge session id through admission (CHE-1049).

The id is validated at ``/api/run``; the queue row must carry it so the owner of
bridge-close handling (CHE-1048) can tell which lease a run holds. Everything
runs against the in-memory queue: no ADB, browser, USB or device.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import DeviceExecutionLock, trace_store


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(session_repo, "db_path", tmp_path / "sessions.db")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    state.queue_items.clear()
    state.draining = False
    monkeypatch.setattr(DeviceExecutionLock, "reserve", MagicMock(return_value="ticket"))
    monkeypatch.setattr(DeviceExecutionLock, "cancel_reservation", MagicMock())
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        TaskQueueService, "_reject_unavailable_device", AsyncMock(return_value=None)
    )
    yield state.queue_items
    state.queue_items.clear()


@pytest.mark.asyncio
async def test_the_bridge_session_id_survives_admission_into_the_queue_row(queue):
    result = await TaskQueueService.enqueue_tasks(
        ["Open Settings"], device_serial="127.0.0.1:41001", bridge_session_id="s41001"
    )

    assert [item["bridge_session_id"] for item in queue] == ["s41001"]
    assert result["tasks"][0]["bridge_session_id"] == "s41001"
    assert queue[0]["device_serial"] == "127.0.0.1:41001"


@pytest.mark.asyncio
async def test_a_run_with_no_bridge_has_no_bridge_session_id(queue):
    await TaskQueueService.enqueue_tasks(["Open Settings"], device_serial="emulator-5554")

    assert queue[0]["bridge_session_id"] is None
