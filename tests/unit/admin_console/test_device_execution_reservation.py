from __future__ import annotations

import asyncio
import json
import io
import sqlite3
from types import SimpleNamespace
import urllib.error
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.device_repository import DeviceRepository
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.services import task_queue_service as queue_module
from apps.admin_console.services.device_identity import device_identity
from apps.admin_console.services.host_registry import host_registry
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceExecutionLock

USB = "phone-usb"
WIFI = "192.168.1.10:5555"
PROPS = {"ro.serialno": "hardware-phone", "ro.product.model": "Pixel"}


@pytest.fixture
def context(tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    repository = SessionRepository(database)
    monkeypatch.setattr(host_registry, "db_path", database)
    monkeypatch.setattr(queue_module, "session_repo", repository)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        TaskQueueService, "_reject_unavailable_device", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(queue_module, "current_adb_endpoint", AdbEndpoint.local)
    monkeypatch.setattr(DeviceExecutionLock, "reserve", MagicMock(return_value="ticket"))
    monkeypatch.setattr(DeviceExecutionLock, "cancel_reservation", MagicMock())
    monkeypatch.setattr(state, "queue_items", [])
    monkeypatch.setattr(state, "active_runs", {})
    monkeypatch.setattr(state, "executing_run_keys", set())
    monkeypatch.setattr(state, "draining", False)
    return repository


def observe(*serials):
    return device_identity.observe_adb(
        AdbEndpoint.local(), [(serial, "device", "Pixel", PROPS) for serial in serials]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["frontend", "sdk", "mcp"])
async def test_concurrent_usb_wifi_accepts_exactly_one_run(context, ingress):
    usb, wifi = observe(USB, WIFI)
    results = await asyncio.gather(
        TaskQueueService.enqueue_tasks(["first"], device_serial=USB, ingress=ingress),
        TaskQueueService.enqueue_tasks(["second"], device_serial=WIFI, ingress=ingress),
        return_exceptions=True,
    )
    accepted = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, AdminAPIError)]
    assert len(accepted) == len(rejected) == 1
    assert rejected[0].code == "device_claimed"
    assert rejected[0].status_code == 409
    assert len(state.queue_items) == 1
    with sqlite3.connect(context.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    item = accepted[0]["tasks"][0]
    row = context.get_session_by_id(item["session_id"])
    assert row["connection_id"] in {usb.connection_id, wifi.connection_id}
    assert json.loads(row["device_info"])["device_binding"] == item["device_binding"]


@pytest.mark.asyncio
async def test_snapshot_survives_worker_upsert_and_other_phones_can_run(context):
    usb, wifi = observe(USB, WIFI)
    first = await TaskQueueService.enqueue_tasks(["first"], device_serial=WIFI)
    session_id = first["tasks"][0]["session_id"]
    from artemis.data_engine.models import SessionMetadata

    StorageManager(context.db_path, context.db_path.parent).create_session(
        SessionMetadata(session_id=session_id, initial_goal="first", device_info={})
    )
    assert context.get_session_by_id(session_id)["connection_id"] == wifi.connection_id
    assert usb.device_id == wifi.device_id
    device_identity.observe_adb(
        AdbEndpoint.local(),
        [("other-phone", "device", "Pixel", {"ro.serialno": "different-hardware"})],
    )
    assert (await TaskQueueService.enqueue_tasks(["other"], device_serial="other-phone"))["tasks"]


@pytest.mark.asyncio
async def test_pending_cancel_releases_claim_but_inflight_stop_does_not(context):
    observe(USB, WIFI)
    first = await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    session_id = first["tasks"][0]["session_id"]
    state.executing_run_keys.add(session_id)
    TaskQueueService._remove_task(session_id)
    with pytest.raises(AdminAPIError, match="reserved"):
        await TaskQueueService.enqueue_tasks(["second"], device_serial=WIFI)
    TaskQueueService._release_run_slot(session_id, session_id, None)
    second = await TaskQueueService.enqueue_tasks(["second"], device_serial=WIFI)
    TaskQueueService._remove_task(second["tasks"][0]["session_id"])
    assert (await TaskQueueService.enqueue_tasks(["third"], device_serial=USB))["tasks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_cleanup", [False, True])
async def test_host_nack_releases_claim_only_if_stop_removed_the_requeued_row(
    context, monkeypatch, stop_before_cleanup
):
    from apps.admin_console.services.host_tunnel import host_tunnels

    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    observe(USB, WIFI)
    device_identity.observe_host(
        "host",
        [
            {
                "serial": "sd-phone",
                "hardware_id": device_identity.hardware_hash(PROPS["ro.serialno"]),
            }
        ],
    )
    endpoint = AdbEndpoint.create("127.0.0.1", 40002, host_id="host", generation=1)
    monkeypatch.setattr(queue_module.host_endpoints, "resolve", lambda host: endpoint)
    monkeypatch.setattr(host_tunnels, "bind_run", MagicMock())
    monkeypatch.setattr(host_tunnels, "release_run", MagicMock())
    monkeypatch.setattr(TaskQueueService, "_run_tasks_by_session", {})
    first = await TaskQueueService.enqueue_tasks(
        ["first"], device_serial="sd-phone", host_id="host"
    )
    item = first["tasks"][0]
    session_id = item["session_id"]
    item["status"] = "starting"
    snapshot_started = asyncio.Event()
    cancellation_started = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def snapshot_for_spawn():
        snapshot_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await finish_cleanup.wait()
            raise

    monkeypatch.setattr(
        "apps.admin_console.services.config_store.get_config_store",
        lambda: SimpleNamespace(snapshot_for_spawn=snapshot_for_spawn),
    )
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    worker = asyncio.create_task(TaskQueueService._execute_task_item(item))
    TaskQueueService._run_tasks_by_session[session_id] = worker
    nack = None
    try:
        await asyncio.wait_for(snapshot_started.wait(), 5)
        nack = asyncio.create_task(TaskQueueService.requeue_starting(session_id))
        await asyncio.wait_for(cancellation_started.wait(), 5)
        assert item["requeue"] is True
        assert session_id in state.executing_run_keys
        if stop_before_cleanup:
            TaskQueueService._remove_stopped_queue_item(session_id, False)
            assert state.queue_items == []
        with pytest.raises(AdminAPIError) as error:
            await TaskQueueService.enqueue_tasks(["during cleanup"], device_serial=WIFI)
        assert error.value.code == "device_claimed"
        finish_cleanup.set()
        assert await asyncio.wait_for(nack, 5) is True
    finally:
        finish_cleanup.set()
        worker.cancel()
        await asyncio.gather(worker, *([nack] if nack else []), return_exceptions=True)

    spawn.assert_not_called()
    assert session_id not in state.executing_run_keys
    assert session_id not in state.active_runs
    if stop_before_cleanup:
        assert state.queue_items == []
        DeviceExecutionLock.cancel_reservation.assert_called_with(item["queue_ticket"])
        assert (await TaskQueueService.enqueue_tasks(["after cleanup"], device_serial=WIFI))[
            "tasks"
        ]
    else:
        assert state.queue_items == [item]
        assert item["status"] == "pending"
        DeviceExecutionLock.cancel_reservation.assert_not_called()
        with pytest.raises(AdminAPIError) as error:
            await TaskQueueService.enqueue_tasks(["after cleanup"], device_serial=WIFI)
        assert error.value.code == "device_claimed"


@pytest.mark.asyncio
async def test_setup_failure_releases_claim_without_a_run(context, monkeypatch):
    observe(USB, WIFI)
    with monkeypatch.context() as patcher:
        patcher.setattr(context, "create_queued_session", lambda *args, **kwargs: False)
        with pytest.raises(RuntimeError, match="persist"):
            await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    assert (await TaskQueueService.enqueue_tasks(["second"], device_serial=WIFI))["tasks"]


@pytest.mark.asyncio
async def test_transport_ticket_failure_rolls_back_the_whole_batch(context, monkeypatch):
    from artemis.runtime.device_lock import DeviceBusyError

    observe(USB, WIFI)
    monkeypatch.setattr(
        DeviceExecutionLock,
        "reserve",
        MagicMock(side_effect=["ticket", DeviceBusyError("ticket failed")]),
    )
    with pytest.raises(DeviceBusyError):
        await TaskQueueService.enqueue_tasks(["one", "two"], device_serial=USB)
    assert state.queue_items == []
    monkeypatch.setattr(DeviceExecutionLock, "reserve", MagicMock(return_value="new-ticket"))
    assert (await TaskQueueService.enqueue_tasks(["third"], device_serial=WIFI))["tasks"]


@pytest.mark.asyncio
async def test_store_failure_is_fail_closed_before_any_ticket(context, monkeypatch):
    from apps.admin_console.database.repositories.device_repository import DeviceStoreNotReady

    monkeypatch.setattr(
        DeviceRepository, "connection", MagicMock(side_effect=DeviceStoreNotReady())
    )
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    assert error.value.status_code == 503
    assert error.value.code == "device_store_not_ready"
    DeviceExecutionLock.reserve.assert_not_called()
    assert state.queue_items == []


@pytest.mark.asyncio
async def test_unobserved_connection_is_reserved_then_follows_resolution(context):
    first = await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    assert context.get_session_by_id(first["tasks"][0]["session_id"])["connection_id"] is None
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["second"], device_serial=USB)
    assert error.value.code == "device_claimed"
    observe(USB, WIFI)
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["third"], device_serial=WIFI)
    assert error.value.code == "device_claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("adb_state,props", [("device", {}), ("unauthorized", {})])
async def test_uncertain_and_provisional_connections_cannot_execute(context, adb_state, props):
    device_identity.observe_adb(AdbEndpoint.local(), [(USB, adb_state, "Pixel", props)])
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    assert error.value.code == "device_identity_unresolved"
    assert state.queue_items == []
    with sqlite3.connect(context.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adb_state,props",
    [("device", {}), ("unauthorized", {}), ("device", {"ro.product.model": "Pixel"})],
)
async def test_known_route_without_fresh_identity_cannot_execute(context, adb_state, props):
    usb, wifi = observe(USB, WIFI)
    assert usb.device_id == wifi.device_id
    latest = device_identity.observe_adb(AdbEndpoint.local(), [(WIFI, adb_state, "Pixel", props)])[
        0
    ]
    assert latest.outcome in {"provisional", "uncertain"}
    stored = DeviceRepository(context.db_path).connection(connection_id=wifi.connection_id)
    assert stored.outcome == "confirmed"

    for _attempt in range(2):
        with pytest.raises(AdminAPIError) as error:
            await TaskQueueService.enqueue_tasks(["unresolved"], device_serial=WIFI)
        assert error.value.code == "device_identity_unresolved"
        assert error.value.status_code == 409
    DeviceExecutionLock.reserve.assert_not_called()
    assert state.queue_items == []
    with sqlite3.connect(context.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    accepted = await TaskQueueService.enqueue_tasks(["confirmed USB"], device_serial=USB)
    session_id = accepted["tasks"][0]["session_id"]
    assert context.get_session_by_id(session_id)["connection_id"] == usb.connection_id
    TaskQueueService._remove_task(session_id)
    assert observe(WIFI)[0].outcome == "confirmed"
    recovered = await TaskQueueService.enqueue_tasks(["confirmed Wi-Fi"], device_serial=WIFI)
    assert recovered["tasks"][0]["connection_id"] == wifi.connection_id


@pytest.mark.asyncio
async def test_unresolved_route_keeps_existing_claim_exclusive_after_alias_resolution(context):
    usb, wifi = observe(USB, WIFI)
    await TaskQueueService.enqueue_tasks(["first"], device_serial=WIFI)
    latest = device_identity.observe_adb(
        AdbEndpoint.local(), [(WIFI, "unauthorized", "Pixel", {})]
    )[0]
    assert latest.outcome == "provisional"
    repo = DeviceRepository(context.db_path)
    canonical = repo.create_device(owner_principal_id=None, label="Pixel", match_state="confirmed")
    repo.add_alias(wifi.device_id, canonical.device_id)
    assert repo.connection(connection_id=usb.connection_id).device_id == canonical.device_id

    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["second"], device_serial=USB)
    assert error.value.code == "device_claimed"
    assert error.value.status_code == 409
    assert len(state.queue_items) == 1


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_one_batch_retains_claim_until_last_run(context):
    observe(USB, WIFI)
    first = await TaskQueueService.enqueue_tasks(["first"], device_serial=USB, session_id="retry")
    retry = await TaskQueueService.enqueue_tasks(["first"], device_serial=USB, session_id="retry")
    assert first["tasks"][0]["session_id"] == retry["tasks"][0]["session_id"]
    assert retry["enqueued_count"] == 0
    TaskQueueService._remove_task("retry")
    batch = await TaskQueueService.enqueue_tasks(["one", "two"], device_serial=USB)
    TaskQueueService._remove_task(batch["tasks"][0]["session_id"])
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["third"], device_serial=WIFI)
    assert error.value.code == "device_claimed"
    TaskQueueService._remove_task(batch["tasks"][1]["session_id"])
    assert (await TaskQueueService.enqueue_tasks(["third"], device_serial=WIFI))["tasks"]


def test_migration_is_additive_idempotent_and_keeps_old_runs_null(tmp_path):
    from artemis.data_engine import run_connections

    database = tmp_path / "old.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('old')")
    report = run_connections.migrate(database)
    assert report.applied == (1,)
    assert report.backup_path.is_file()
    assert run_connections.migrate(database).applied == ()
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT connection_id FROM sessions WHERE session_id = 'old'"
        ).fetchone() == (None,)


def test_uncertain_records_share_a_reservation_key(context):
    from apps.admin_console.services.device_reservation import device_reservations
    from artemis.runtime.run_device_binding import RunDeviceBinding
    from artemis.runtime.adb_endpoint import AdbTarget

    device_identity.observe_adb(
        AdbEndpoint.local(),
        [(serial, "device", "Pixel", {"ro.product.model": "Pixel"}) for serial in (USB, WIFI)],
    )
    bindings = [RunDeviceBinding(AdbTarget(AdbEndpoint.local(), serial)) for serial in (USB, WIFI)]
    snapshots = [device_reservations.snapshot(binding, context.db_path) for binding in bindings]
    assert snapshots[0].key == snapshots[1].key


def test_connection_lookup_resolves_aliases(context):
    repo = DeviceRepository(context.db_path)
    usb, wifi = observe(USB, WIFI)
    assert repo.connection(connection_id=usb.connection_id).device_id == wifi.device_id


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["frontend", "sdk", "mcp"])
async def test_run_route_returns_device_claimed_for_the_loser(context, admin, monkeypatch, ingress):
    from apps.admin_console.routers import tasks as tasks_router
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    observe(USB, WIFI)
    monkeypatch.setattr(run_catalog_repo, "db_path", context.db_path)
    monkeypatch.setattr(tasks_router, "session_repo", context)
    monkeypatch.setattr(
        tasks_router.device_pool, "validate_explicit_serial_async", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        tasks_router.readiness_engine, "run_device_submission_probe", AsyncMock(return_value=None)
    )
    first, second = await asyncio.gather(
        admin.post("/api/run", json={"goal": "first", "device_serial": USB, "ingress": ingress}),
        admin.post("/api/run", json={"goal": "second", "device_serial": WIFI, "ingress": ingress}),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["code"] == "device_claimed"


@pytest.mark.asyncio
async def test_host_and_browser_share_the_local_phone_reservation(context, monkeypatch):
    from apps.admin_console.services import device_identity as identity_module
    from apps.admin_console.services.bridge_session_service import bridge_session_service

    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    bridge_serial = "127.0.0.1:40001"
    monkeypatch.setattr(identity_module, "_bridge_serials", lambda: {bridge_serial})
    monkeypatch.setattr(
        bridge_session_service,
        "live_sessions",
        lambda: [SimpleNamespace(serial=bridge_serial, session_id="browser", revoked=False)],
    )
    observe(USB, bridge_serial)
    device_identity.observe_host(
        "host",
        [
            {
                "serial": "sd-phone",
                "hardware_id": device_identity.hardware_hash(PROPS["ro.serialno"]),
            }
        ],
    )
    endpoint = AdbEndpoint.create("127.0.0.1", 40002, host_id="host", generation=1)
    monkeypatch.setattr(queue_module.host_endpoints, "resolve", lambda host: endpoint)
    await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    for serial, kwargs in [
        (bridge_serial, {"bridge_session_id": "browser"}),
        ("sd-phone", {"host_id": "host"}),
    ]:
        with pytest.raises(AdminAPIError) as error:
            await TaskQueueService.enqueue_tasks(["second"], device_serial=serial, **kwargs)
        assert error.value.code == "device_claimed"


@pytest.mark.asyncio
async def test_alias_during_claim_keeps_one_reservation_and_the_original_snapshot(context):
    repo = DeviceRepository(context.db_path)
    usb, wifi = observe(USB, WIFI)
    first = await TaskQueueService.enqueue_tasks(["first"], device_serial=USB)
    target = repo.create_device(owner_principal_id=None, label="canonical", match_state="confirmed")
    repo.add_alias(usb.device_id, target.device_id)
    with pytest.raises(AdminAPIError) as error:
        await TaskQueueService.enqueue_tasks(["second"], device_serial=WIFI)
    assert error.value.code == "device_claimed"
    assert repo.connection(connection_id=wifi.connection_id).device_id == target.device_id
    assert (
        context.get_session_by_id(first["tasks"][0]["session_id"])["connection_id"]
        == usb.connection_id
    )


def test_daemon_client_preserves_the_claim_error_and_mcp_returns_it(context, monkeypatch):
    from artemis.runtime import daemon_client
    from mcp_server.tools import task_runner

    payload = {"detail": "The phone is already reserved.", "code": "device_claimed", "fix": "Wait."}
    error = urllib.error.HTTPError(
        "http://localhost/api/run", 409, "Conflict", {}, io.BytesIO(json.dumps(payload).encode())
    )
    monkeypatch.setattr(daemon_client.urllib.request, "urlopen", MagicMock(side_effect=error))
    response = daemon_client.submit_task_to_daemon("second")
    assert response["status"] == "rejected"
    assert response["code"] == "device_claimed"
    monkeypatch.setattr(
        task_runner, "ensure_daemon_running", lambda **kwargs: (True, "http://localhost")
    )
    monkeypatch.setattr(task_runner, "submit_task_to_daemon", lambda **kwargs: response)
    monkeypatch.setattr(
        task_runner.subprocess, "Popen", MagicMock(side_effect=AssertionError("must not fall back"))
    )
    result = task_runner.mobile_run_task("second", "reservation-test")
    assert result["status"] == "failed"
    assert result["code"] == "device_claimed"
