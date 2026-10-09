"""A run bound to a browser phone keeps its bridge session id through admission (CHE-1049).

The id is validated at ``/api/run``; the queue row must carry it so the owner of
bridge-close handling (CHE-1048) can tell which lease a run holds. Everything
runs against the in-memory queue: no ADB, browser, USB or device.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.admin_console.core.ownership import SYSTEM_PRINCIPAL
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import tasks
from apps.admin_console.schemas.task_schema import RunRequest
from apps.admin_console.services.bridge_session_service import BridgeSession, BridgeSessionService
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime import AdbEndpoint, DeviceExecutionLock, trace_store


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
async def test_the_bridge_session_id_survives_admission_into_the_queue_row(queue, monkeypatch):
    bridge = BridgeSessionService()
    lease = BridgeSession("s41001", port=41001, expires_at=time.monotonic() + 60)
    bridge._sessions[lease.session_id] = lease
    monkeypatch.setattr(
        "apps.admin_console.services.bridge_session_service.bridge_session_service", bridge
    )
    monkeypatch.setattr(
        "apps.admin_console.services.task_queue_service.current_adb_endpoint", AdbEndpoint.local
    )
    result = await TaskQueueService.enqueue_tasks(
        ["Open Settings"], device_serial="127.0.0.1:41001", bridge_session_id="s41001"
    )

    assert [item["bridge_session_id"] for item in queue] == ["s41001"]
    assert result["tasks"][0]["bridge_session_id"] == "s41001"
    assert queue[0]["device_serial"] == "127.0.0.1:41001"
    assert queue[0]["device_binding"]["bridge_session_id"] == "s41001"


@pytest.mark.asyncio
async def test_a_run_with_no_bridge_has_no_bridge_session_id(queue):
    await TaskQueueService.enqueue_tasks(["Open Settings"], device_serial="emulator-5554")

    assert queue[0]["bridge_session_id"] is None


@pytest.mark.asyncio
async def test_merged_router_uses_bound_browser_serial_before_device_selection(monkeypatch):
    serial = "127.0.0.1:41001"
    monkeypatch.setattr(
        tasks.bridge_session_service,
        "get",
        AsyncMock(return_value=SimpleNamespace(serial=serial, revoked=False, is_expired=False)),
    )
    monkeypatch.setattr(tasks, "own_default_serial", MagicMock(return_value="other-phone"))
    monkeypatch.setattr(tasks, "require_device", MagicMock())
    preferred_validation = AsyncMock(side_effect=AssertionError("wrong ADB endpoint"))
    monkeypatch.setattr(tasks.device_pool, "validate_explicit_serial_async", preferred_validation)
    local_validation = AsyncMock(return_value=None)
    local_pool = SimpleNamespace(validate_explicit_serial_async=local_validation)
    pool_for = MagicMock(return_value=local_pool)
    monkeypatch.setattr(tasks.device_pool, "pool_for", pool_for)
    probe = AsyncMock(return_value=SimpleNamespace(summary="Ready", metadata={}))
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", probe)
    enqueue = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(tasks.task_queue_service, "require_admission_open", MagicMock())

    await tasks.run_task(
        RunRequest(goal="Open Settings", bridge_session_id="browser-session"), SYSTEM_PRINCIPAL
    )

    assert probe.await_args.kwargs["target_serial"] == serial
    assert probe.await_args.kwargs["endpoint"] == AdbEndpoint.local()
    pool_for.assert_called_once_with(AdbEndpoint.local())
    local_validation.assert_awaited_once_with(serial)
    preferred_validation.assert_not_awaited()
    assert enqueue.await_args.kwargs["device_serial"] == serial
    assert enqueue.await_args.kwargs["bridge_session_id"] == "browser-session"


def test_browser_and_host_references_cannot_bind_the_same_request():
    with pytest.raises(ValueError, match="mutually exclusive"):
        RunRequest(
            goal="Open Settings",
            bridge_session_id="browser-session",
            device_ref={"host_id": "host-a", "serial": "USB123"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing", "revoked", "expired"])
async def test_bridge_binding_rejection_identifies_the_http_path(monkeypatch, caplog, reason):
    session = (
        None
        if reason == "missing"
        else SimpleNamespace(revoked=reason == "revoked", is_expired=reason == "expired")
    )
    monkeypatch.setattr(tasks.bridge_session_service, "get", AsyncMock(return_value=session))
    request = RunRequest(
        goal="private prompt", device_serial="127.0.0.1:41001", bridge_session_id="old-lease"
    )

    with pytest.raises(AdminAPIError) as rejection:
        await tasks._bind_bridge_session(request)

    assert rejection.value.status_code == 409
    assert rejection.value.code == "bridge_session_unavailable"
    assert "event=bridge_run_bind_rejected" in caplog.text
    assert "bridge_session_id=old-lease" in caplog.text
    assert f"reason={reason}" in caplog.text
    assert "private prompt" not in caplog.text


@pytest.mark.asyncio
async def test_bridge_queue_rejection_identifies_the_queue_path(queue, caplog):
    with pytest.raises(AdminAPIError) as rejection:
        await TaskQueueService.enqueue_tasks(
            ["private prompt"], device_serial="127.0.0.1:41001", bridge_session_id="old-lease"
        )

    assert rejection.value.status_code == 409
    assert rejection.value.code == "bridge_queue_binding_unavailable"
    assert "event=bridge_queue_admission_rejected" in caplog.text
    assert "bridge_session_id=old-lease" in caplog.text
    assert "device_serial=127.0.0.1:41001" in caplog.text
    assert "private prompt" not in caplog.text
    assert queue == []
