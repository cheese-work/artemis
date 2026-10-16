"""Browser-phone ownership at the server API seam (CHE-1153, slice O3 of CHE-1150).

A browser phone belongs to the verified identity that connected it. In
cloudflare mode a QA sees and may run on their own browser phones plus shared
devices (anything that is not a bridge address). An admin sees and uses all.
Open mode never filters. A test token *is* the email: the verifier fake echoes
it back as the claim.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import AccessConfig, AccessIdentity
from apps.admin_console.routers import device_bridge
from apps.admin_console.routers import hosts as hosts_router
from apps.admin_console.routers import system as system_router
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.server import app
from apps.admin_console.services.bridge_session_service import (
    BridgeSession,
    bridge_session_service,
)
from apps.admin_console.services.host_registry import host_registry
from apps.admin_console.services.task_queue_service import task_queue_service
from artemis.core.diagnostics.schema import (
    DeviceInfo,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)
from artemis.runtime.device_pool import DeviceStatus

QA1 = "qa1@example.com"
QA2 = "qa2@example.com"
ADMIN = "admin@example.com"

QA1_PHONE = "127.0.0.1:41001"
QA1_SECOND_PHONE = "127.0.0.1:41003"
QA2_PHONE = "127.0.0.1:41002"
ORPHAN_PHONE = "127.0.0.1:41009"  # connected with no signed-in identity
SHARED = "emulator-5554"  # an X99 device: shared with everyone


def _use_cloudflare(monkeypatch) -> None:
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({ADMIN}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token, "sub": token})
    monkeypatch.setattr(app.state, "access_verifier", verifier)


def _sessions(monkeypatch) -> None:
    """Two QAs' phones (one has two) and one with no owner, in the live registry."""
    phones = {
        QA1_PHONE: QA1,
        QA1_SECOND_PHONE: QA1,
        QA2_PHONE: QA2,
        ORPHAN_PHONE: None,
    }
    sessions = {
        serial: BridgeSession(
            session_id=f"s{serial.rsplit(':', 1)[1]}",
            port=int(serial.rsplit(":", 1)[1]),
            created_at=float(order),
            expires_at=float("inf"),
            owner=owner,
        )
        for order, (serial, owner) in enumerate(phones.items())
    }
    monkeypatch.setattr(
        bridge_session_service, "_sessions", {s.session_id: s for s in sessions.values()}
    )


def _pool(monkeypatch) -> None:
    """The one shared adb server lists every phone, like production."""
    devices = [
        DeviceStatus(serial=SHARED, state="device", model="Pixel 8", device_kind="emulator"),
        DeviceStatus(serial=QA1_PHONE, state="device", model="21081111RG", device_kind="phone"),
        DeviceStatus(serial=QA1_SECOND_PHONE, state="device", model="Pixel 6", device_kind="phone"),
        DeviceStatus(serial=QA2_PHONE, state="device", model="SM-S911B", device_kind="phone"),
        DeviceStatus(serial=ORPHAN_PHONE, state="device", model=None),
    ]
    monkeypatch.setattr(
        tasks_router.device_pool, "list_devices_async", AsyncMock(return_value=devices)
    )
    monkeypatch.setattr(
        hosts_router.device_pool, "list_devices_async", AsyncMock(return_value=devices)
    )


@pytest.fixture
def cloudflare(monkeypatch):
    _use_cloudflare(monkeypatch)
    _sessions(monkeypatch)
    _pool(monkeypatch)


@pytest.fixture
def open_mode(monkeypatch):
    monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))
    _sessions(monkeypatch)
    _pool(monkeypatch)


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
        base_url="http://localhost",
    )


async def _get(email: str | None, path: str):
    async with _client() as client:
        return await client.get(path, headers={"Cf-Access-Jwt-Assertion": email} if email else {})


async def _run(email: str | None, **body):
    async with _client() as client:
        return await client.post(
            "/api/run",
            json={"goal": "x", **body},
            headers={"Cf-Access-Jwt-Assertion": email} if email else {},
        )


# -- device list: /api/devices -----------------------------------------------------


async def _serials(email: str | None, path: str = "/api/devices") -> set[str]:
    response = await _get(email, path)
    assert response.status_code == 200, response.text
    return {d["serial"] for d in response.json()["devices"]}


@pytest.mark.asyncio
async def test_a_qa_sees_only_their_own_browser_phones_and_shared_devices(cloudflare):
    assert await _serials(QA1) == {SHARED, QA1_PHONE, QA1_SECOND_PHONE}
    assert await _serials(QA2) == {SHARED, QA2_PHONE}


@pytest.mark.asyncio
async def test_connecting_a_second_browser_phone_keeps_both_listed(cloudflare):
    assert {QA1_PHONE, QA1_SECOND_PHONE} <= await _serials(QA1)


@pytest.mark.asyncio
async def test_a_browser_phone_with_no_owner_is_admin_only(cloudflare):
    assert ORPHAN_PHONE not in await _serials(QA1)
    assert ORPHAN_PHONE not in await _serials(None)
    assert ORPHAN_PHONE in await _serials(ADMIN)


@pytest.mark.asyncio
async def test_a_bridge_address_the_server_does_not_know_is_hidden_from_a_qa(
    cloudflare, monkeypatch
):
    stale = DeviceStatus(serial="127.0.0.1:49999", state="device")
    monkeypatch.setattr(
        tasks_router.device_pool, "list_devices_async", AsyncMock(return_value=[stale])
    )
    assert await _serials(QA1) == set()
    assert await _serials(ADMIN) == {"127.0.0.1:49999"}


@pytest.mark.asyncio
async def test_a_shared_device_is_visible_to_every_qa(cloudflare):
    assert SHARED in await _serials(QA1)
    assert SHARED in await _serials(QA2)


@pytest.mark.asyncio
async def test_an_admin_sees_every_device(cloudflare):
    assert await _serials(ADMIN) == {
        SHARED,
        QA1_PHONE,
        QA1_SECOND_PHONE,
        QA2_PHONE,
        ORPHAN_PHONE,
    }


@pytest.mark.asyncio
async def test_open_mode_lists_every_device_unfiltered(open_mode):
    assert await _serials(None) == {
        SHARED,
        QA1_PHONE,
        QA1_SECOND_PHONE,
        QA2_PHONE,
        ORPHAN_PHONE,
    }


# -- computer registry list: /api/hosts ----------------------------------------------


@pytest.fixture
def hosts_enabled(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "enabled")
    shared = {
        "serial": "LAB-PIXEL",
        "model": "Pixel 7",
        "device_kind": "phone",
        "source": "computer",
        "computer_id": "h1",
        "computer_name": "Lab computer",
        "computer_status": "online",
        "reason": None,
        "since": None,
    }
    monkeypatch.setattr(host_registry, "list_hosts", lambda: ([], [shared]))


def _live_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge_session_service,
        "live_sessions",
        lambda: list(bridge_session_service._sessions.values()),
    )


@pytest.mark.asyncio
async def test_computers_list_shows_a_qa_only_their_own_browser_phones(
    cloudflare, hosts_enabled, monkeypatch
):
    _live_sessions(monkeypatch)
    assert await _serials(QA1, "/api/hosts") == {"LAB-PIXEL", QA1_PHONE, QA1_SECOND_PHONE}
    assert await _serials(QA2, "/api/hosts") == {"LAB-PIXEL", QA2_PHONE}


@pytest.mark.asyncio
async def test_computers_list_shows_an_admin_everything_and_names_each_owner(
    cloudflare, hosts_enabled, monkeypatch
):
    _live_sessions(monkeypatch)
    response = await _get(ADMIN, "/api/hosts")
    by_serial = {d["serial"]: d for d in response.json()["devices"]}

    assert set(by_serial) == {
        "LAB-PIXEL",
        QA1_PHONE,
        QA1_SECOND_PHONE,
        QA2_PHONE,
        ORPHAN_PHONE,
    }
    assert by_serial[QA2_PHONE]["owner"] == QA2
    assert by_serial[QA2_PHONE]["model"] == "SM-S911B"


@pytest.mark.asyncio
async def test_computers_list_names_a_qa_as_the_owner_of_their_own_phone(
    cloudflare, hosts_enabled, monkeypatch
):
    _live_sessions(monkeypatch)
    response = await _get(QA1, "/api/hosts")
    mine = next(d for d in response.json()["devices"] if d["serial"] == QA1_PHONE)
    assert mine["owner"] == QA1


@pytest.mark.asyncio
async def test_computers_list_never_leaks_another_qas_address(
    cloudflare, hosts_enabled, monkeypatch
):
    _live_sessions(monkeypatch)
    text = (await _get(QA1, "/api/hosts")).text
    assert QA2_PHONE not in text and ORPHAN_PHONE not in text and QA2 not in text


# -- readiness report: what the Setup page and device picker read ---------------------


def _report() -> SystemReadinessReport:
    devices = [
        DeviceInfo(serial=s, model=m)
        for s, m in (
            (SHARED, "Pixel 8"),
            (QA1_PHONE, "21081111RG"),
            (QA2_PHONE, "SM-S911B"),
        )
    ]
    adb = ProbeResult(
        id="android_adb",
        category=ProbeCategory.DEVICE,
        title="Device / Emulator Connected",
        status=ProbeStatus.PASS,
        summary="Connected",
        description=f"Active device {QA2_PHONE}; 3 devices attached.",
        metadata={
            "devices": [d.model_dump() for d in devices],
            "device_count": 3,
            "active_device": devices[2].model_dump(),
        },
    )
    return SystemReadinessReport(
        overall_ready=True,
        blocker_count=1,
        passed_blocker_count=1,
        probes=[adb],
        active_device=devices[2],
        timestamp=1.0,
    )


@pytest.mark.asyncio
async def test_readiness_lists_a_qa_only_their_own_and_shared_devices(cloudflare, monkeypatch):
    monkeypatch.setattr(
        system_router.readiness_engine, "run_all", AsyncMock(return_value=_report())
    )

    body = (await _get(QA1, "/api/system/readiness")).json()
    listed = {d["serial"] for d in body["probes"][0]["metadata"]["devices"]}

    assert listed == {SHARED, QA1_PHONE}
    assert body["probes"][0]["metadata"]["device_count"] == 2


@pytest.mark.asyncio
async def test_readiness_never_shows_a_qa_another_qas_address_or_active_device(
    cloudflare, monkeypatch
):
    monkeypatch.setattr(
        system_router.readiness_engine, "run_all", AsyncMock(return_value=_report())
    )

    response = await _get(QA1, "/api/system/readiness")

    assert QA2_PHONE not in response.text
    assert response.json()["active_device"] is None
    assert response.json()["probes"][0]["metadata"]["active_device"] is None


@pytest.mark.asyncio
async def test_readiness_shows_an_admin_every_device(cloudflare, monkeypatch):
    monkeypatch.setattr(
        system_router.readiness_engine, "run_all", AsyncMock(return_value=_report())
    )

    body = (await _get(ADMIN, "/api/system/readiness")).json()

    assert {d["serial"] for d in body["probes"][0]["metadata"]["devices"]} == {
        SHARED,
        QA1_PHONE,
        QA2_PHONE,
    }
    assert body["active_device"]["serial"] == QA2_PHONE


@pytest.mark.asyncio
async def test_open_mode_readiness_is_unfiltered(open_mode, monkeypatch):
    monkeypatch.setattr(
        system_router.readiness_engine, "run_all", AsyncMock(return_value=_report())
    )

    body = (await _get(None, "/api/system/readiness")).json()

    assert body["active_device"]["serial"] == QA2_PHONE
    assert len(body["probes"][0]["metadata"]["devices"]) == 3


# -- /api/run: the server enforces the rule, the UI only reflects it -----------------


@pytest.fixture
def submit(monkeypatch):
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    probe = AsyncMock(return_value=None)
    validate = AsyncMock(return_value=None)
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(tasks_router.readiness_engine, "run_device_submission_probe", probe)
    monkeypatch.setattr(tasks_router.device_pool, "validate_explicit_serial_async", validate)
    return SimpleNamespace(enqueue=enqueue, probe=probe, validate=validate)


def _assert_no_side_effect(submit) -> None:
    submit.enqueue.assert_not_awaited()
    submit.probe.assert_not_awaited()
    submit.validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_on_another_qas_phone_is_refused_with_no_side_effect(cloudflare, submit):
    response = await _run(QA1, device_serial=QA2_PHONE)

    assert response.status_code == 403
    assert response.json()["code"] == "device_not_yours"
    assert QA2_PHONE not in response.text
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_run_on_an_unowned_browser_phone_is_refused_for_a_qa(cloudflare, submit):
    response = await _run(QA1, device_serial=ORPHAN_PHONE)

    assert response.status_code == 403
    assert response.json()["code"] == "device_not_yours"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_run_with_no_identity_on_a_browser_phone_is_refused(cloudflare, submit):
    response = await _run(None, device_serial=QA1_PHONE)

    assert response.status_code == 403
    assert response.json()["code"] == "device_not_yours"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
@pytest.mark.parametrize("serial", [QA1_PHONE, QA1_SECOND_PHONE, SHARED])
async def test_run_on_own_or_shared_device_is_accepted(cloudflare, submit, serial):
    response = await _run(QA1, device_serial=serial)

    assert response.status_code == 200
    assert submit.enqueue.await_args.kwargs["device_serial"] == serial


@pytest.mark.asyncio
async def test_run_on_a_shared_device_is_accepted_for_the_other_qa_too(cloudflare, submit):
    assert (await _run(QA2, device_serial=SHARED)).status_code == 200


@pytest.mark.asyncio
async def test_an_admin_may_run_on_any_phone(cloudflare, submit):
    response = await _run(ADMIN, device_serial=QA2_PHONE)

    assert response.status_code == 200
    assert submit.enqueue.await_args.kwargs["device_serial"] == QA2_PHONE


@pytest.mark.asyncio
async def test_open_mode_run_on_any_phone_is_accepted(open_mode, submit):
    assert (await _run(None, device_serial=QA2_PHONE)).status_code == 200


@pytest.mark.asyncio
async def test_run_with_no_device_prefers_the_callers_own_browser_phone(cloudflare, submit):
    response = await _run(QA2)

    assert response.status_code == 200
    assert submit.probe.await_args.kwargs["target_serial"] == QA2_PHONE
    assert submit.enqueue.await_args.kwargs["device_serial"] == QA2_PHONE


@pytest.mark.asyncio
async def test_run_with_two_phones_and_no_device_prefers_the_newest(cloudflare, submit):
    await _run(QA1)

    assert submit.probe.await_args.kwargs["target_serial"] == QA1_SECOND_PHONE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alias",
    [
        " " + QA2_PHONE,  # leading space
        QA2_PHONE + " ",  # trailing space
        QA2_PHONE.replace(":", ";"),  # the pool maps every non-word character to "_"
        QA2_PHONE.replace(".", "_").replace(":", "_"),
        "LOCALHOST:41002",  # case is not a way out either
    ],
)
async def test_a_spelling_variant_of_another_qas_phone_is_refused_with_no_side_effect(
    cloudflare, submit, alias
):
    response = await _run(QA1, device_serial=alias)

    assert response.status_code == 403
    assert response.json()["code"] == "device_not_yours"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_a_bridge_address_with_no_session_is_refused_for_a_qa(cloudflare, submit):
    response = await _run(QA1, device_serial="127.0.0.1:49999")

    assert response.status_code == 403
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_a_spelling_variant_of_your_own_phone_stays_yours(cloudflare, submit):
    assert (await _run(QA1, device_serial=" " + QA1_PHONE)).status_code == 200


# -- auto-selection only ever considers the caller's own and shared devices --------------


@pytest.fixture
def adb(monkeypatch):
    """The real submission probe over a fake adb: (serial, state) list and locked serials."""
    probe = tasks_router.readiness_engine._adb_probe
    monkeypatch.setattr("artemis.toolchain.toolchain.resolve", lambda _name: "adb")
    monkeypatch.setattr(probe, "_target_serial", None)
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)

    def configure(states, locked=()):
        monkeypatch.setattr(probe, "_get_device_states", AsyncMock(return_value=states))

        async def lock_state(_adb, serial, timeout_seconds=1.0):
            return serial in locked

        monkeypatch.setattr(probe, "_get_confirmed_device_lock_state", lock_state)
        return enqueue

    return configure


@pytest.mark.asyncio
async def test_with_no_phone_of_their_own_a_qa_gets_the_shared_device_not_a_foreign_phone(
    cloudflare, adb
):
    enqueue = adb([(QA2_PHONE, "device"), (SHARED, "device")])

    response = await _run("qa3@example.com")  # owns no phone

    assert response.status_code == 200
    assert enqueue.await_args.kwargs["device_serial"] == SHARED


@pytest.mark.asyncio
async def test_a_locked_shared_device_is_reported_without_naming_a_foreign_phone(cloudflare, adb):
    enqueue = adb([(QA2_PHONE, "device"), (SHARED, "device")], locked={SHARED})

    response = await _run("qa3@example.com")

    assert response.status_code == 409
    assert SHARED in response.json()["detail"]
    assert QA2_PHONE not in response.text
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_locked_foreign_phone_never_leaks_its_address_to_a_qa(cloudflare, adb):
    enqueue = adb([(QA2_PHONE, "device")], locked={QA2_PHONE})

    response = await _run("qa3@example.com")

    assert response.status_code == 409
    assert QA2_PHONE not in response.text
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_with_no_allowed_device_at_all_the_run_is_refused_not_queued_on_a_foreign_phone(
    cloudflare, adb
):
    enqueue = adb([(QA2_PHONE, "device")])

    response = await _run("qa3@example.com")

    assert response.status_code == 409
    assert response.json()["code"] == "no_device_available"
    assert QA2_PHONE not in response.text
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admin_with_no_phone_named_still_auto_selects_across_every_device(cloudflare, adb):
    enqueue = adb([(QA2_PHONE, "device"), (SHARED, "device")])

    response = await _run(ADMIN)

    assert response.status_code == 200
    assert enqueue.await_args.kwargs["device_serial"] == QA2_PHONE


@pytest.mark.asyncio
async def test_open_mode_auto_selection_is_unchanged(open_mode, adb):
    enqueue = adb([(QA2_PHONE, "device"), (SHARED, "device")])

    assert (await _run(None)).status_code == 200
    assert enqueue.await_args.kwargs["device_serial"] == QA2_PHONE


# -- the bridge records who connected the phone ---------------------------------------


@pytest.mark.asyncio
async def test_create_session_records_the_owner(monkeypatch):
    from apps.admin_console.services.bridge_session_service import BridgeSessionService

    service = BridgeSessionService()
    session = await service.create_session(owner=QA1)
    try:
        assert session.owner == QA1
        assert service.owner_of(session.serial) == QA1
        assert service.owner_of("127.0.0.1:1") is None
    finally:
        await service.revoke(session.session_id)


class _Websocket:
    """Just enough of a loopback WebSocket for the handler to lease one session."""

    def __init__(self, identity: AccessIdentity | None) -> None:
        self.scope = {"client": ("127.0.0.1", 50000)}
        self.client = ("127.0.0.1", 50000)
        self.state = SimpleNamespace(identity=identity)
        self.messages: list = []

    async def accept(self):
        pass

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code):
        pass

    async def receive(self):
        return {"type": "websocket.disconnect"}


@pytest.mark.asyncio
async def test_the_handler_leases_the_session_to_the_verified_identity(monkeypatch):
    seen = {}

    class Service:
        async def create_session(self, owner=None):
            seen["owner"] = owner
            return BridgeSession(session_id="s", port=4000, expires_at=float("inf"))

        async def connect(self, session):
            return session.serial

        async def revoke(self, _session_id):
            pass

    monkeypatch.setattr(device_bridge, "bridge_session_service", Service())

    identity = AccessIdentity(email=QA1, admin=False, auth_mode="cloudflare")
    await device_bridge.open_bridge_session(_Websocket(identity))
    assert seen["owner"] == QA1

    await device_bridge.open_bridge_session(_Websocket(None))
    assert seen["owner"] is None


# -- /api/run with bridge_session_id: the run is bound to the phone its bridge holds -------


@pytest.mark.asyncio
async def test_run_bound_to_a_live_bridge_targets_that_phone(cloudflare, submit):
    response = await _run(QA1, bridge_session_id="s41001")

    assert response.status_code == 200
    assert submit.enqueue.await_args.kwargs["device_serial"] == QA1_PHONE
    # The validated lease travels with the run, not just its serial.
    assert submit.enqueue.await_args.kwargs["bridge_session_id"] == "s41001"


@pytest.mark.asyncio
async def test_run_with_no_bridge_enqueues_no_bridge_session_id(cloudflare, submit):
    assert (await _run(QA1, device_serial=SHARED)).status_code == 200
    assert submit.enqueue.await_args.kwargs["bridge_session_id"] is None


@pytest.mark.asyncio
async def test_run_bound_to_a_bridge_accepts_the_serial_that_bridge_holds(cloudflare, submit):
    response = await _run(QA1, bridge_session_id="s41001", device_serial=QA1_PHONE)

    assert response.status_code == 200
    assert submit.enqueue.await_args.kwargs["device_serial"] == QA1_PHONE


@pytest.mark.asyncio
async def test_run_bound_to_a_bridge_that_is_gone_is_refused_not_rerouted(cloudflare, submit):
    response = await _run(QA1, bridge_session_id="s-closed", device_serial=SHARED)

    assert response.status_code == 409
    assert response.json()["code"] == "bridge_session_unavailable"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_run_bound_to_an_expired_bridge_is_refused(cloudflare, submit):
    bridge_session_service._sessions["s41001"].expires_at = 0.0

    response = await _run(QA1, bridge_session_id="s41001")

    assert response.status_code == 409
    assert response.json()["code"] == "bridge_session_unavailable"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_run_naming_another_phone_than_its_bridge_is_refused(cloudflare, submit):
    response = await _run(QA1, bridge_session_id="s41001", device_serial=SHARED)

    assert response.status_code == 409
    assert response.json()["code"] == "device_mismatch"
    _assert_no_side_effect(submit)


@pytest.mark.asyncio
async def test_run_bound_to_another_qas_bridge_is_refused(cloudflare, submit):
    response = await _run(QA1, bridge_session_id="s41002")

    assert response.status_code == 403
    assert response.json()["code"] == "device_not_yours"
    _assert_no_side_effect(submit)
