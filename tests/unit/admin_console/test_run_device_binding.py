"""Cross-transport identity and loss controls use only fake phones and processes."""

from dataclasses import dataclass
import time
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from adbutils import AdbClient
import pytest

from apps.admin_console.core.state import state
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.schemas.task_schema import RunRequest
from apps.admin_console.services import bridge_session_service as bridge_module
from apps.admin_console.services import host_tunnel as tunnel_module
from apps.admin_console.services import host_registry as registry_module
from apps.admin_console.services import task_queue_service as queue_module
from apps.admin_console.services.bridge_session_service import BridgeSession, BridgeSessionService
from apps.admin_console.services.host_tunnel import HostTunnels
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.data_engine.storage import StorageManager
from artemis.data_engine.models import SessionMetadata
from artemis.runtime import trace_store
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.host_endpoints import HostEndpointRegistry, HostOffline
from artemis.runtime.host_protocol import CONTRACT
from artemis.runtime.lifecycle import LifecycleAuthority
from artemis.runtime.run_device_binding import RunDeviceBinding
from artemis.runtime.adb_endpoint import AdbTarget


@dataclass
class Clock:
    now: float = 100

    def __call__(self):
        return self.now


class Socket:
    async def send_bytes(self, payload):
        pass


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    repository = SessionRepository(database)
    clock = Clock()
    authority = LifecycleAuthority(database, clock=clock)
    monkeypatch.setattr(SessionRepository, "lifecycle", property(lambda repository: authority))
    monkeypatch.setattr(queue_module, "session_repo", repository)
    monkeypatch.setattr(DeviceExecutionLock, "reserve", lambda **kwargs: "fake-ticket")
    monkeypatch.setattr(DeviceExecutionLock, "cancel_reservation", lambda ticket: None)
    bridge = BridgeSessionService()
    monkeypatch.setattr(bridge_module, "bridge_session_service", bridge)
    monkeypatch.setattr(
        "admin_console.services.bridge_session_service.bridge_session_service", bridge
    )
    hosts = HostTunnels(
        endpoints=HostEndpointRegistry(), clock=clock, set_status=lambda *args: None
    )
    monkeypatch.setattr(tunnel_module, "host_tunnels", hosts)
    monkeypatch.setattr(queue_module, "host_endpoints", hosts.endpoints)
    monkeypatch.setattr(state, "queue_items", [])
    monkeypatch.setattr(state, "active_runs", {})
    return SimpleNamespace(
        bridge=bridge, hosts=hosts, clock=clock, repository=repository, authority=authority
    )


def queue_item(context, endpoint, serial, *, host_id=None, session_id="run"):
    item = TaskQueueService._create_queue_item(
        "One fake step",
        0,
        1.0,
        endpoint,
        session_id,
        "flash",
        None,
        None,
        None,
        None,
        serial,
        "frontend",
        None,
        host_id=host_id,
    )
    context.repository.create_queued_session(
        session_id, item["goal"], "flash", serial, 1.0, device_binding=item["device_binding"]
    )
    state.queue_items.append(item)
    if host_id:
        context.hosts.bind_run(host_id, session_id)
    return item


def fake_process(session_id="run"):
    process = MagicMock()
    process.returncode = None
    state.active_runs[session_id] = {"process": process, "session_id": session_id}
    return process


@pytest.mark.asyncio
async def test_host_enqueue_snapshots_registered_endpoint_not_local_preference(
    context, monkeypatch
):
    endpoint = AdbEndpoint.create("127.0.0.1", 31415, host_id="host", generation=4)
    context.hosts.endpoints.register(endpoint)
    local_preference = MagicMock(return_value=AdbEndpoint.local())
    monkeypatch.setattr(queue_module, "current_adb_endpoint", local_preference)
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())

    result = await TaskQueueService.enqueue_tasks(
        ["One fake step"], host_id="host", device_serial="selected", session_id="run"
    )

    (item,) = result["tasks"]
    expected_binding = RunDeviceBinding(AdbTarget(endpoint, "selected", "host")).to_dict()
    assert item["adb_endpoint"] == endpoint.to_dict()
    assert item["device_binding"] == expected_binding
    persisted = context.repository.read_session("run")
    assert json.loads(persisted["device_info"])["device_binding"] == expected_binding
    assert TaskQueueService._task_target(item).endpoint == endpoint


@pytest.mark.asyncio
async def test_host_enqueue_requires_registered_endpoint_before_reservation(context, monkeypatch):
    reserve = MagicMock()
    monkeypatch.setattr(DeviceExecutionLock, "reserve", reserve)
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())

    with pytest.raises(HostOffline):
        await TaskQueueService.enqueue_tasks(
            ["One fake step"], host_id="host", device_serial="selected"
        )

    reserve.assert_not_called()
    assert state.queue_items == []


@pytest.mark.asyncio
async def test_queued_host_retry_reuses_binding_without_live_endpoint(context, monkeypatch):
    endpoint = AdbEndpoint.create("127.0.0.1", 31415, host_id="host", generation=4)
    context.hosts.endpoints.register(endpoint)
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    accepted = await TaskQueueService.enqueue_tasks(
        ["One fake step"], host_id="host", device_serial="selected", session_id="run"
    )
    context.hosts.endpoints.unregister("host", 4)

    retry = await TaskQueueService.enqueue_tasks(
        ["One fake step"], host_id="host", device_serial="selected", session_id="run"
    )

    assert retry["enqueued_count"] == 0
    assert retry["tasks"][0]["device_binding"] == accepted["tasks"][0]["device_binding"]
    assert len(state.queue_items) == 1


def browser_lease(context, serial, lease_id="browser-lease"):
    session = BridgeSession(
        lease_id, port=int(serial.split(":")[-1]), expires_at=time.monotonic() + 60
    )
    context.bridge._sessions[lease_id] = session
    return session


def test_selected_fake_device_is_not_retargeted(context, fake_adb_server_factory):
    selected = fake_adb_server_factory("selected")
    wrong = fake_adb_server_factory("wrong")
    selected.add_device("same-serial").shell_handler = lambda device, command: b"selected"
    wrong.add_device("same-serial").shell_handler = lambda device, command: b"wrong"
    item = queue_item(context, selected.endpoint, "same-serial")
    target = TaskQueueService._task_target(item)
    assert (
        AdbClient(target.endpoint.host, target.endpoint.port).device(target.serial).shell("who")
        == "selected"
    )
    assert wrong.requests == []
    item["adb_endpoint"] = wrong.endpoint.to_dict()
    with pytest.raises(RuntimeError, match="bound|binding|identity"):
        TaskQueueService._task_target(item)


def test_changed_device_serial_is_rejected(context, fake_adb_server_factory):
    server = fake_adb_server_factory()
    item = queue_item(context, server.endpoint, "selected")
    item["device_serial"] = "wrong"
    with pytest.raises(RuntimeError, match="bound|binding|identity"):
        TaskQueueService._task_target(item)


def test_browser_lease_is_snapshotted_without_host_serial_aliasing(context, monkeypatch):
    local = AdbEndpoint.local()
    lease = browser_lease(context, "127.0.0.1:31415")
    browser = queue_item(context, local, lease.serial, session_id="browser-run")
    assert browser.get("bridge_session_id") == lease.session_id
    host = AdbEndpoint.create("127.0.0.1", 31416, host_id="host", generation=1)
    hosted = queue_item(context, host, lease.serial, host_id="host", session_id="host-run")
    assert hosted.get("bridge_session_id") is None
    assert browser["device_binding"]["device_lease"] != hosted["device_binding"]["device_lease"]


def test_explicit_bridge_id_selects_its_lease_among_same_serial_leases(context):
    browser_lease(context, "127.0.0.1:31415", "first-lease")
    second = browser_lease(context, "127.0.0.1:31415", "second-lease")
    item = TaskQueueService._create_queue_item(
        "One fake step",
        0,
        1.0,
        AdbEndpoint.local(),
        "run",
        "flash",
        None,
        None,
        None,
        None,
        second.serial,
        "frontend",
        None,
        bridge_session_id="second-lease",
    )
    assert item["bridge_session_id"] == "second-lease"
    assert item["device_binding"]["bridge_session_id"] == "second-lease"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_exists", [True, False])
async def test_explicit_bridge_lease_lost_after_admission_is_rejected_before_acceptance(
    context, monkeypatch, replacement_exists
):
    selected = browser_lease(context, "127.0.0.1:31415", "selected-lease")
    request = RunRequest(
        goal="One fake step", session_id="run", bridge_session_id=selected.session_id
    )
    monkeypatch.setattr(tasks_router, "bridge_session_service", context.bridge)
    await tasks_router._bind_bridge_session(request)
    assert request.device_serial == selected.serial

    async def replace_lease_during_readiness(serial, endpoint):
        selected.revoked = True
        context.bridge._sessions.pop(selected.session_id)
        if replacement_exists:
            browser_lease(context, selected.serial, "replacement-lease")
        return None

    reservation = MagicMock(return_value="fake-ticket")
    persistence = MagicMock(wraps=context.repository.create_queued_session)
    trace = MagicMock(wraps=trace_store.init_trace)
    monkeypatch.setattr(DeviceExecutionLock, "reserve", reservation)
    monkeypatch.setattr(context.repository, "create_queued_session", persistence)
    monkeypatch.setattr(trace_store, "init_trace", trace)
    monkeypatch.setattr(queue_module, "current_adb_endpoint", AdbEndpoint.local)
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        TaskQueueService, "_reject_unavailable_device", replace_lease_during_readiness
    )

    with pytest.raises(AdminAPIError) as rejection:
        await TaskQueueService.enqueue_tasks(
            [request.goal],
            device_serial=request.device_serial,
            session_id=request.session_id,
            bridge_session_id=request.bridge_session_id,
        )

    assert rejection.value.status_code == 409
    assert rejection.value.code == "device_offline"
    reservation.assert_not_called()
    persistence.assert_not_called()
    trace.assert_not_called()
    assert context.repository.read_session("run") is None
    assert state.queue_items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
async def test_closed_browser_interrupts_only_its_bound_run_once(context, host):
    first = browser_lease(context, "127.0.0.1:31415", "first-lease")
    second = browser_lease(context, "127.0.0.1:31416", "second-lease")
    endpoint = AdbEndpoint.create(host, 5037)
    item = queue_item(context, endpoint, first.serial)
    queue_item(context, endpoint, second.serial, session_id="other")
    assert item["bridge_session_id"] == first.session_id
    assert item["adb_endpoint"] == endpoint.to_dict()
    assert item["device_binding"]["endpoint"] == endpoint.to_dict()
    persisted = context.repository.read_session("run")
    assert json.loads(persisted["device_info"])["device_binding"] == item["device_binding"]
    process = fake_process()
    other = fake_process("other")
    await context.bridge.revoke(first.session_id)
    await context.bridge.revoke(first.session_id)
    assert context.repository.get_session_status("run") == "interrupted"
    assert context.repository.get_session_status("other") == "queued"
    assert len(context.authority.pending_events("run")) == 1
    process.kill.assert_called_once()
    other.kill.assert_not_called()
    with pytest.raises(RuntimeError, match="interrupted|bound|binding"):
        TaskQueueService._task_target(state.queue_items[0], resolve_host=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "port", "host_id"),
    [
        ("127.0.0.1", 5038, None),
        ("remote.example", 5037, None),
        ("127.0.0.1", 5037, "host"),
    ],
)
async def test_nonlocal_or_host_endpoint_does_not_capture_browser_lease(
    context, host, port, host_id
):
    lease = browser_lease(context, "127.0.0.1:31415")
    endpoint = AdbEndpoint.create(host, port, host_id=host_id)
    item = queue_item(context, endpoint, lease.serial, host_id=host_id)
    assert item["bridge_session_id"] is None
    assert item["device_binding"]["bridge_session_id"] is None
    assert item["adb_endpoint"] == endpoint.to_dict()
    assert item["device_binding"]["endpoint"] == endpoint.to_dict()
    process = fake_process()
    await context.bridge.revoke(lease.session_id)
    assert context.repository.get_session_status("run") == "queued"
    assert context.authority.pending_events("run") == []
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_reused_browser_serial_cannot_replace_the_accepted_lease(context):
    first = browser_lease(context, "127.0.0.1:31415", "first-lease")
    item = queue_item(context, AdbEndpoint.local(), first.serial)
    await context.bridge.revoke(first.session_id)
    browser_lease(context, first.serial, "replacement-lease")
    with pytest.raises(RuntimeError, match="interrupted|bound|binding"):
        TaskQueueService._task_target(item, resolve_host=True)


@pytest.mark.asyncio
async def test_same_serial_on_other_host_cannot_replace_binding(context):
    try:
        first = await context.hosts.attach("first", 1, Socket(), lambda: {"same-serial"})
        second = await context.hosts.attach("second", 1, Socket(), lambda: {"same-serial"})
        item = queue_item(
            context, context.hosts.endpoints.resolve("first"), "same-serial", host_id="first"
        )
        item["host_id"] = "second"
        item["adb_endpoint"] = context.hosts.endpoints.resolve("second").to_dict()
        with pytest.raises(RuntimeError, match="bound|binding|identity"):
            TaskQueueService._task_target(item, resolve_host=True)
        assert first.host_id != second.host_id
    finally:
        await context.hosts.close()


@pytest.mark.asyncio
async def test_same_device_recovers_within_grace_and_ignores_stale_close(context):
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"phone"})
        item = queue_item(context, context.hosts.endpoints.resolve("host"), "phone", host_id="host")
        snapshot = item["adb_endpoint"].copy()
        await context.hosts.disconnect("host", 1, "disconnected")
        context.clock.now += CONTRACT.grace_seconds - 1
        await context.hosts.attach("host", 2, Socket(), lambda: {"phone"})
        await context.hosts.disconnect("host", 1, "timeout")
        context.clock.now += 2
        context.hosts.expire()
        target = TaskQueueService._task_target(item, resolve_host=True)
        assert (target.host_id, target.serial, target.endpoint.generation) == ("host", "phone", 2)
        assert item["adb_endpoint"] == snapshot
        assert context.repository.get_session_status("run") == "queued"
        assert context.authority.pending_events("run") == []
    finally:
        await context.hosts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnected", "auth_expired"])
async def test_host_fatal_loss_or_grace_expiry_stops_once(context, reason):
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"phone"})
        item = queue_item(context, context.hosts.endpoints.resolve("host"), "phone", host_id="host")
        process = fake_process()
        await context.hosts.disconnect("host", 1, reason)
        if reason == "disconnected":
            assert context.authority.pending_events("run") == []
            context.clock.now += CONTRACT.grace_seconds
            context.hosts.expire()
            context.hosts.expire()
        assert context.repository.get_session_status("run") == "interrupted"
        assert len(context.authority.pending_events("run")) == 1
        process.kill.assert_called_once()
        with pytest.raises(RuntimeError, match="interrupted|bound|binding"):
            TaskQueueService._task_target(item, resolve_host=True)
    finally:
        await context.hosts.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("in_grace", [False, True])
async def test_host_unenroll_stops_its_bound_run_once(context, in_grace):
    # main's /api/agent/unenroll aborts the host; a bound run must stop exactly once.
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"phone"})
        item = queue_item(context, context.hosts.endpoints.resolve("host"), "phone", host_id="host")
        process = fake_process()
        if in_grace:
            await context.hosts.disconnect("host", 1, "disconnected")
            assert context.authority.pending_events("run") == []
        await context.hosts.abort_host("host", "auth_expired")
        await context.hosts.abort_host("host", "auth_expired")
        assert context.repository.get_session_status("run") == "interrupted"
        assert len(context.authority.pending_events("run")) == 1
        process.kill.assert_called_once()
        with pytest.raises(RuntimeError, match="interrupted|bound|binding"):
            TaskQueueService._task_target(item, resolve_host=True)
    finally:
        await context.hosts.close()


@pytest.mark.asyncio
async def test_reconnect_with_a_different_device_is_terminal(context):
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"selected"})
        queue_item(context, context.hosts.endpoints.resolve("host"), "selected", host_id="host")
        await context.hosts.disconnect("host", 1, "disconnected")
        context.clock.now += 1
        await context.hosts.attach("host", 2, Socket(), lambda: {"wrong"})
        assert context.repository.get_session_status("run") == "interrupted"
        assert len(context.authority.pending_events("run")) == 1
    finally:
        await context.hosts.close()


@pytest.mark.parametrize("method", ["create_session", "update_session"])
def test_worker_metadata_cannot_replace_accepted_identity(context, method):
    item = queue_item(context, AdbEndpoint.local(), "selected")
    storage = StorageManager(context.repository.db_path, context.repository.db_path.parent)
    metadata = SessionMetadata(
        session_id="run",
        initial_goal="One fake step",
        device_info={"device_id": "selected", "device_binding": {"serial": "wrong"}},
    )
    getattr(storage, method)(metadata)
    stored = json.loads(context.repository.read_session("run")["device_info"])
    assert stored["device_binding"] == item["device_binding"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_committed_completion_or_cancellation_beats_browser_loss(context, status):
    lease = browser_lease(context, "127.0.0.1:31415")
    queue_item(context, AdbEndpoint.local(), lease.serial)
    process = fake_process()
    context.authority.finish("run", status)
    await context.bridge.revoke(lease.session_id)
    assert context.repository.get_session_status("run") == status
    assert len(context.authority.pending_events("run")) == 1
    process.kill.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_between_validation_and_worker_registration_stops_launch(context):
    lease = browser_lease(context, "127.0.0.1:31415")
    item = queue_item(context, AdbEndpoint.local(), lease.serial)
    TaskQueueService._task_target(item, resolve_host=True)
    await context.bridge.revoke(lease.session_id)
    process = fake_process()
    await TaskQueueService._terminate_if_cancelled_during_launch("run", "run", process)
    process.kill.assert_called_once()
    assert context.repository.get_session_status("run") == "interrupted"


@pytest.mark.asyncio
async def test_removed_queue_item_releases_host_binding(context):
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"phone"})
        queue_item(context, context.hosts.endpoints.resolve("host"), "phone", host_id="host")
        TaskQueueService._remove_task("run")
        assert "run" not in context.hosts.runs.get("host", set())
    finally:
        await context.hosts.close()


@pytest.mark.asyncio
async def test_current_device_inventory_loss_is_terminal_but_stale_inventory_is_not(
    context, monkeypatch
):
    def connection(_db_path=None):
        database = sqlite3.connect(context.repository.db_path)
        database.row_factory = sqlite3.Row
        return database

    monkeypatch.setattr(registry_module, "get_db", connection)
    registry = registry_module.HostRegistry()
    try:
        await context.hosts.attach("host", 1, Socket(), lambda: {"selected"})
        queue_item(context, context.hosts.endpoints.resolve("host"), "selected", host_id="host")
        process = fake_process()
        with registry._db() as database:
            database.execute(
                "INSERT INTO hosts (id,name,public_key,key_hash,created_by,created_at,status,since,generation) "
                "VALUES ('host','fake','','test-key','test',1,'online',1,1)"
            )
        assert registry.set_devices("host", 0, []) is False
        assert context.repository.get_session_status("run") == "queued"
        process.kill.assert_not_called()
        assert registry.set_devices("host", 1, []) is True
        assert context.repository.get_session_status("run") == "interrupted"
        assert len(context.authority.pending_events("run")) == 1
        process.kill.assert_called_once()
    finally:
        await context.hosts.close()


def test_binding_requires_selected_device():
    with pytest.raises(ValueError, match="device"):
        RunDeviceBinding(AdbTarget(AdbEndpoint.local(), None))


def test_durable_binding_read_failure_is_scoped_to_the_run(context, monkeypatch):
    item = queue_item(context, AdbEndpoint.local(), "selected")
    monkeypatch.setattr(
        context.repository, "read_session", MagicMock(side_effect=OSError("unreadable"))
    )
    assert TaskQueueService._task_target(item).serial == "selected"
    with pytest.raises(RuntimeError, match="admission"):
        TaskQueueService._task_target(item, resolve_host=True)


def test_missing_durable_admission_cannot_launch(context, monkeypatch):
    item = queue_item(context, AdbEndpoint.local(), "selected")
    monkeypatch.setattr(context.repository, "read_session", lambda session_id: None)
    with pytest.raises(RuntimeError, match="durable admission"):
        TaskQueueService._task_target(item, resolve_host=True)


@pytest.mark.asyncio
async def test_launch_read_failure_stops_only_the_bound_worker(context, monkeypatch):
    queue_item(context, AdbEndpoint.local(), "selected")
    process = fake_process()
    other = fake_process("other")
    monkeypatch.setattr(
        context.repository, "read_session", MagicMock(side_effect=OSError("unreadable"))
    )
    with pytest.raises(RuntimeError, match="admission"):
        await TaskQueueService._terminate_if_cancelled_during_launch("run", "run", process)
    process.kill.assert_called_once()
    other.kill.assert_not_called()


def test_recovery_rejects_stale_generation(context):
    accepted = AdbEndpoint.create("127.0.0.1", 31415, host_id="host", generation=2)
    stale = AdbEndpoint.create("127.0.0.1", 31416, host_id="host", generation=1)
    binding = RunDeviceBinding(AdbTarget(accepted, "selected", "host"))
    with pytest.raises(ValueError, match="generation"):
        binding.require_selection(AdbTarget(stale, "selected", "host"), recovery=True)


@pytest.mark.asyncio
async def test_unresolved_device_is_rejected_before_reservation(context, monkeypatch):
    from artemis.runtime.device_pool import device_pool

    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", lambda: None)
    monkeypatch.setattr(TaskQueueService, "require_admission_open", lambda: None)
    monkeypatch.setattr(device_pool, "select_device_async", AsyncMock(return_value=None))
    reserve = MagicMock()
    monkeypatch.setattr(DeviceExecutionLock, "reserve", reserve)
    result = await TaskQueueService.enqueue_tasks(["One fake step"])
    assert result["status"] == "rejected"
    assert state.queue_items == []
    reserve.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("host_id", [None, "host"])
@pytest.mark.parametrize("requested_serial", ["selected", "wrong"])
async def test_durable_retry_reuses_only_accepted_device(
    context, monkeypatch, host_id, requested_serial
):
    from apps.admin_console.core.ownership import OwnerScope
    from apps.admin_console.routers import tasks as task_router
    from apps.admin_console.schemas.task_schema import DeviceRef, RunRequest
    from apps.admin_console.core.access_control import AdminAPIError

    endpoint = AdbEndpoint.create("127.0.0.1", 31415, host_id=host_id, generation=1)
    item = queue_item(context, endpoint, "selected", host_id=host_id)
    state.queue_items.clear()
    monkeypatch.setattr(task_router, "session_repo", context.repository)
    probe = AsyncMock(side_effect=AssertionError("Retry must not probe devices"))
    monkeypatch.setattr(task_router.readiness_engine, "run_device_submission_probe", probe)
    request = RunRequest(
        goal="One fake step",
        session_id="run",
        **(
            {"device_ref": DeviceRef(host_id=host_id, serial=requested_serial)}
            if host_id
            else {"device_serial": requested_serial}
        ),
    )
    if requested_serial == "wrong":
        with pytest.raises(AdminAPIError) as rejected:
            await task_router.run_task(request, OwnerScope(enforced=False))
        assert rejected.value.status_code == 409
    else:
        result = await task_router.run_task(request, OwnerScope(enforced=False))
        assert result["tasks"][0]["device_binding"] == item["device_binding"]
        assert result["tasks"][0]["host_id"] == host_id
    probe.assert_not_awaited()


def test_binding_captures_browser_lease_from_the_server_import_path(context, monkeypatch):
    from admin_console.services import bridge_session_service as server_bridge

    primary = BridgeSessionService()
    lease = BridgeSession("server-lease", port=31415, expires_at=time.monotonic() + 60)
    primary._sessions[lease.session_id] = lease
    monkeypatch.setattr(server_bridge, "bridge_session_service", primary)
    item = queue_item(context, AdbEndpoint.local(), lease.serial)
    assert item["bridge_session_id"] == lease.session_id
    assert TaskQueueService._task_target(item, resolve_host=True).serial == lease.serial
