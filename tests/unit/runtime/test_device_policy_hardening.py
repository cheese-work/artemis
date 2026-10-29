# ruff: noqa: F811  (fixtures imported from test_device_policy are re-requested by name)
"""Review blockers on the restricted device policy (CHE-2129, review of PR #245).

Each test reproduces one confirmed bypass without a device. Fake-server tests send real
``adb`` traffic to a fake adb server and assert what the server *received*; spy tests assert
zero calls on the device transport.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.runtime import device_policy, helper_stream
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_policy import (
    AuditLog,
    DevicePolicyDenied,
    LeftTestApp,
    restrict,
)
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.runtime.helper_manager import DEVICE_PORT
from tests.support.fake_adb import http_service
from tests.unit.runtime.test_device_policy import (
    APK,
    ESCAPE,
    PKG,
    SERIAL,
    UID,
    _resumed,
    sink,  # noqa: F401  (fixture)
    transport,  # noqa: F401  (fixture)
)

needs_adb = pytest.mark.skipif(shutil.which("adb") is None, reason="adb binary not installed")
OTHER = "UNRESTRICTED-PHONE"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEVICE_POLICY", "on")
    yield
    device_policy.reset()


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


def _restrict(transport, audit, **kw):
    return restrict(
        transport,
        SERIAL,
        test_packages=frozenset({PKG, ESCAPE}),
        test_uid=UID,
        locked_package=PKG,
        apk_paths=frozenset({APK}),
        run_id="run-1",
        audit=audit,
        **kw,
    )


@pytest.fixture
def device(transport, audit):  # noqa: F811
    return _restrict(transport, audit)


@pytest.fixture
def guest(fake_adb_server_factory, audit):
    """A fake adb server with the restricted phone; ``log`` holds every shell line it ran."""
    server = fake_adb_server_factory("guest-phone")
    log: list[str] = []
    state = {"foreground": PKG}

    def shell(_device, command: str) -> bytes:
        log.append(command)
        if "dumpsys activity activities" in command:
            return _resumed(state["foreground"])
        return b"ok\n"

    server.add_device(SERIAL, shell_handler=shell, screencap=b"\x89PNG-guest")
    server.add_device(OTHER, shell_handler=shell)
    transport = EndpointTransport(server.endpoint)
    device = _restrict(transport, audit)
    return SimpleNamespace(
        server=server, transport=transport, device=device, log=log, state=state, audit=audit
    )


def _forbidden_seen(guest) -> bool:
    return any("pm list" in c or "pm clear" in c for c in guest.log)


# ------------------------------------------------- blocker 3: a second serial option


@pytest.mark.parametrize(
    "prefix",
    [
        ["-s", OTHER, "-s", SERIAL],
        ["-s", SERIAL, "-s", OTHER],
        ["-H", "10.0.0.9", "-s", SERIAL],
        ["-s", SERIAL, "-P", "5038"],
        ["-d"],
        ["-t", "1"],
    ],
    ids=lambda p: " ".join(p),
)
def test_extra_adb_selectors_are_refused_before_the_transport(
    device, transport, sink, audit, prefix
):  # noqa: F811
    with pytest.raises(DevicePolicyDenied):
        transport.run([*prefix, "shell", "pm", "list", "packages"])

    assert sink.calls == []
    assert [e["decision"] for e in audit.entries()] == ["denied"]


@needs_adb
def test_a_later_serial_option_never_reaches_the_fake_server(guest):
    with pytest.raises(DevicePolicyDenied):
        guest.transport.run(
            ["-s", OTHER, "-s", SERIAL, "shell", "pm", "list", "packages"], capture_output=True
        )

    assert not _forbidden_seen(guest)


# ---------------------------------------------------- blocker 2: raw adbutils handles


@needs_adb
def test_a_raw_client_device_cannot_run_a_shell_on_the_restricted_phone(guest):
    with pytest.raises(DevicePolicyDenied):
        guest.transport.client().device(SERIAL).shell("pm list packages")

    assert not _forbidden_seen(guest)
    assert [e["decision"] for e in guest.audit.entries()] == ["denied"]


@needs_adb
def test_devices_from_the_device_list_cannot_run_a_shell_on_the_restricted_phone(guest):
    for found in guest.transport.device_list():
        if found.serial == SERIAL:
            with pytest.raises(DevicePolicyDenied):
                found.shell("pm list packages")

    assert not _forbidden_seen(guest)


@needs_adb
def test_an_unrestricted_phone_behind_the_same_client_is_untouched(guest):
    out = guest.transport.client().device(OTHER).shell("pm list packages")

    assert out.strip() == "ok"
    assert guest.log == ["pm list packages"]


@needs_adb
def test_a_transport_id_selector_cannot_name_the_restricted_phone(guest):
    transport_id = guest.server.devices[SERIAL].transport_id
    with pytest.raises(DevicePolicyDenied):
        guest.transport.client().device(transport_id=transport_id).shell("pm list packages")

    assert not _forbidden_seen(guest)


# ------------------------------------------------------ blocker 4: helper streams


def _snapshot_service(guest, body=b'{"screenshot": "SECRET-FRAME"}'):
    def handler(_method, _body):
        return 200, body

    guest.server.devices[SERIAL].services[DEVICE_PORT] = http_service(
        {"/snapshot": handler, "/ping": handler, "/action": handler, "/other": handler}
    )


@needs_adb
def test_a_helper_snapshot_is_refused_and_withheld_when_the_app_has_left(guest):
    _snapshot_service(guest)
    guest.state["foreground"] = "com.other.bank"

    with pytest.raises(LeftTestApp):
        helper_stream.request(guest.transport, SERIAL, DEVICE_PORT, "GET", "/snapshot?fields=xml")

    assert guest.transport.open_stream_count == 0
    assert not any(r.request.startswith("tcp:") for r in guest.server.requests)


@needs_adb
def test_a_helper_snapshot_in_the_test_app_is_audited_and_returned(guest):
    _snapshot_service(guest)

    body = helper_stream.request(guest.transport, SERIAL, DEVICE_PORT, "GET", "/snapshot")

    assert b"SECRET-FRAME" in body
    commands = [e["command"] for e in guest.audit.entries() if e["decision"] == "allowed"]
    assert "helper GET /snapshot" in commands


@needs_adb
def test_a_snapshot_is_discarded_when_the_app_leaves_while_it_is_taken(guest):
    def leave(_method, _body):
        guest.state["foreground"] = "com.other.bank"
        return 200, b'{"screenshot": "SECRET-FRAME"}'

    guest.server.devices[SERIAL].services[DEVICE_PORT] = http_service({"/snapshot": leave})

    with pytest.raises(LeftTestApp):
        helper_stream.request(guest.transport, SERIAL, DEVICE_PORT, "GET", "/snapshot")


@needs_adb
@pytest.mark.parametrize("method,path", [("GET", "/other"), ("POST", "/snapshot"), ("GET", "/x")])
def test_other_helper_routes_are_denied_before_any_stream(guest, method, path):
    _snapshot_service(guest)

    with pytest.raises(DevicePolicyDenied):
        helper_stream.request(guest.transport, SERIAL, DEVICE_PORT, method, path)

    assert not any(r.request.startswith("tcp:") for r in guest.server.requests)


@needs_adb
def test_a_raw_stream_to_the_restricted_phone_is_refused(guest):
    _snapshot_service(guest)

    with pytest.raises(DevicePolicyDenied):
        guest.transport.open_stream(SERIAL, DEVICE_PORT)

    assert guest.transport.open_stream_count == 0


@needs_adb
def test_a_helper_request_to_an_unrestricted_phone_is_unchanged(guest):
    guest.server.devices[OTHER].services[DEVICE_PORT] = http_service(
        {"/snapshot": lambda _m, _b: (200, b"plain")}
    )

    assert (
        helper_stream.request(guest.transport, OTHER, DEVICE_PORT, "GET", "/snapshot") == b"plain"
    )
    assert guest.audit.entries() == []


# ------------------------------------------------- blocker 5: single-use screenshot permit


def test_a_finished_capture_leaves_no_permit_for_a_raw_screencap(device, transport, sink, tmp_path):  # noqa: F811
    device.capture_screenshot(tmp_path / "1.png")
    sink.foreground = "com.other.bank"
    before = len(sink.calls)

    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, "exec-out", "screencap", "-p"])

    assert len(sink.calls) == before


def test_every_screencap_follows_its_own_foreground_check_under_concurrency(device, sink, tmp_path):  # noqa: F811
    errors: list[BaseException] = []

    def capture(n: int) -> None:
        try:
            for i in range(10):
                device.capture_screenshot(tmp_path / f"{n}-{i}.png")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=capture, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    kinds = ["dump" if "dumpsys" in " ".join(c) else "cap" for c in sink.calls]
    assert kinds == ["dump", "cap", "dump"] * 40


# ------------------------------------------------------- blocker 6: settings tracking


@pytest.mark.parametrize(
    "command",
    [
        ["shell", "settings", "put", "global", "window_animation_scale", "0"],
        ["shell", "cmd", "uimode", "night", "yes"],
    ],
    ids=["settings put", "uimode night yes"],
)
def test_a_direct_settings_write_is_refused_before_the_transport(device, transport, sink, command):  # noqa: F811
    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, *command])

    assert sink.calls == []


def test_tracked_writes_are_restored_by_the_run_teardown(device, transport, sink, audit):  # noqa: F811
    sink.replies["settings get global window_animation_scale"] = b"1.0\n"
    device.apply_setting("window_animation_scale", "0")

    device_policy.finish_run(device)

    puts = [c[-5:] for c in sink.calls if "put" in c]
    assert ["settings", "put", "global", "window_animation_scale", "1.0"] in puts

    sink.calls.clear()  # registry released: a raw command now passes
    transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"])
    assert len(sink.calls) == 1
    assert "denied" not in [e["decision"] for e in audit.entries()]


def test_a_tracked_write_cannot_be_replayed_with_other_arguments(device, transport, sink):  # noqa: F811
    sink.replies["settings get global window_animation_scale"] = b"1.0\n"
    device.apply_setting("window_animation_scale", "0")
    before = len(sink.calls)

    with pytest.raises(DevicePolicyDenied):
        transport.run(
            ["-s", SERIAL, "shell", "settings", "put", "global", "window_animation_scale", "9"]
        )

    assert len(sink.calls) == before


# --------------------------------------------- blocker 1: activation in the run lifecycle


@pytest.fixture
def lease_db(tmp_path, monkeypatch):
    from apps.admin_console.services import device_leases as leases
    from artemis.data_engine.storage import StorageManager

    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    monkeypatch.setattr("artemis.runtime.device_policy.lease_db_path", lambda: path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO devices (device_id, owner_principal_id, label, hardware_hash, match_state,"
            " created_at, updated_at) VALUES ('dev_guest', NULL, 'guest', NULL, 'confirmed', 1, 1)"
        )
        conn.execute(
            "INSERT INTO device_connections (connection_id, device_id, kind, source, host_id,"
            " serial, created_at) VALUES ('c1', 'dev_guest', 'usb', 'local', NULL, ?, 1)",
            (SERIAL,),
        )
    return SimpleNamespace(path=path, leases=leases)


def test_a_restricted_lease_activates_the_policy_for_its_serial(lease_db, transport, sink):  # noqa: F811
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)

    active = device_policy.activate_for_run(transport, SERIAL, run_id="run-9")

    assert active is not None
    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"])
    assert sink.calls == []


def test_an_unrestricted_lease_leaves_the_serial_alone(lease_db, transport, sink):  # noqa: F811
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run")

    assert device_policy.activate_for_run(transport, SERIAL, run_id="run-9") is None
    transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"])
    assert len(sink.calls) == 1


def test_an_expired_restricted_lease_does_not_restrict(lease_db, transport, sink, monkeypatch):  # noqa: F811
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 1, "run", restricted=True)
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 10**10)

    assert device_policy.activate_for_run(transport, SERIAL, run_id="run-9") is None


def test_the_flag_off_activates_nothing(lease_db, transport, monkeypatch):  # noqa: F811
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    monkeypatch.delenv("ARTEMIS_DEVICE_POLICY")

    assert device_policy.activate_for_run(transport, SERIAL, run_id="run-9") is None


def test_the_env_list_restricts_a_serial_without_a_lease(lease_db, transport, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ARTEMIS_RESTRICTED_SERIALS", f"a, {SERIAL}")

    assert device_policy.activate_for_run(transport, SERIAL, run_id="run-9") is not None


def test_an_unreadable_lease_store_fails_closed(transport, monkeypatch, tmp_path):  # noqa: F811
    bad = tmp_path / "not-a-db"
    bad.write_bytes(b"this is not sqlite" * 100)
    monkeypatch.setattr("artemis.runtime.device_policy.lease_db_path", lambda: bad)

    with pytest.raises(sqlite3.DatabaseError):
        device_policy.activate_for_run(transport, SERIAL, run_id="run-9")


@pytest.fixture
def agent(monkeypatch, transport, sink, lease_db):  # noqa: F811
    from artemis.sdk.agent import Agent
    from third_party.mobile_use.sdk.agent import AgentBase

    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    seen = SimpleNamespace(ran=AsyncMock(return_value="done"))
    monkeypatch.setattr(AgentBase, "_run_task", lambda self, request: seen.ran(request))
    instance = Agent.__new__(Agent)  # skip the config/LLM setup: only the device seams matter
    instance._initialized = True
    instance._adb_client = MagicMock()
    instance._ui_adb_client = MagicMock(disconnect=MagicMock())
    instance._transport = transport
    instance._device_serial = SERIAL
    instance.seen = seen
    return instance


@pytest.mark.asyncio
async def test_a_run_on_a_restricted_phone_is_stopped_with_left_test_app(agent, sink, tmp_path):  # noqa: F811
    request = SimpleNamespace(locked_app_package=PKG, app_path=None, goal="open escape")
    agent.activate_device_policy(SERIAL, run_id="run-9", receipt_dir=tmp_path)

    async def tap_escape(req):  # the run takes a step, then the escape activity is in front
        sink.foreground = ESCAPE.replace(".escape", ".elsewhere")
        with pytest.raises(LeftTestApp):
            agent._device_policy.capture_screenshot(tmp_path / "step.png")
        return "done"

    agent.seen.ran.side_effect = tap_escape

    with pytest.raises(LeftTestApp):
        await agent._run_task(request)

    assert not (tmp_path / "step.png").exists()


@pytest.mark.asyncio
async def test_a_restricted_run_without_a_locked_app_is_refused_before_it_starts(agent, tmp_path):
    request = SimpleNamespace(locked_app_package=None, app_path=None, goal="anything")
    agent.activate_device_policy(SERIAL, run_id="run-9", receipt_dir=tmp_path)

    with pytest.raises(DevicePolicyDenied):
        await agent._run_task(request)

    agent.seen.ran.assert_not_called()


@pytest.mark.asyncio
async def test_clean_restores_settings_exports_the_receipt_and_lifts_the_policy(
    agent, sink, tmp_path
):  # noqa: F811
    sink.replies["settings get global window_animation_scale"] = b"1.0\n"
    agent.activate_device_policy(SERIAL, run_id="run-9", receipt_dir=tmp_path)
    agent._device_policy.apply_setting("window_animation_scale", "0")

    await agent.clean()

    receipt = json.loads((tmp_path / "command_receipt.json").read_text())
    assert receipt["run_id"] == "run-9"
    assert any(e["command"].startswith("shell settings put") for e in receipt["entries"])
    assert ["settings", "put", "global", "window_animation_scale", "1.0"] in [
        c[-5:] for c in sink.calls
    ]
    sink.calls.clear()
    EndpointTransport(AdbEndpoint.create("127.0.0.1", 5037), adb_path="adb").run(
        ["-s", SERIAL, "shell", "pm", "list", "packages"]
    )
    assert len(sink.calls) == 1
