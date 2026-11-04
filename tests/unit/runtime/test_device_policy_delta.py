# ruff: noqa: F811  (fixtures imported from the sibling test modules are re-requested by name)
"""Delta-review blockers R1 and R4 on the restricted device policy (CHE-2129, PR #245).

R1: the policy is on before initialization touches the phone, and a left-app stop refuses every
later command. R4: the helper stream carries only the helper's own port and only the input
commands a run needs (no clipboard, no global action but ``back``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.runtime import device_policy, helper_stream
from artemis.runtime.device_policy import DevicePolicyDenied, LeftTestApp
from artemis.runtime.helper_manager import DEVICE_PORT
from tests.support.fake_adb import http_service
from tests.unit.runtime.test_device_policy import ESCAPE, PKG, SERIAL, sink, transport  # noqa: F401
from tests.unit.runtime.test_device_policy_hardening import (  # noqa: F401  (fixtures)
    _clean_registry,
    agent,
    audit,
    device,
    guest,
    lease_db,
    needs_adb,
)
from tests.unit.runtime.test_device_policy_hardening import OTHER  # noqa: E402

ELSEWHERE = "com.other.bank"
OTHER_PORT = 19999


def _stop(device, sink):
    """Latch the stop the way the run does: the foreground is not the test app."""
    sink.foreground = ELSEWHERE
    with pytest.raises(LeftTestApp):
        device.check_foreground()
    sink.calls.clear()


# ---------------------------------------------- R1: a stop refuses every later command


@pytest.mark.parametrize(
    "argv",
    [
        ["shell", "am", "start", "-n", f"{PKG}/.MainActivity"],
        ["shell", "dumpsys", "activity", "activities"],
        ["shell", "cmd", "uimode", "night"],
    ],
    ids=["start-test-app", "foreground-query", "night-query"],
)
def test_a_stopped_device_refuses_every_later_command(device, transport, sink, argv):  # noqa: F811
    _stop(device, sink)

    with pytest.raises(LeftTestApp):
        transport.run(["-s", SERIAL, *argv], capture_output=True)

    assert sink.calls == []


def test_a_stopped_device_takes_no_new_setting(device, sink):  # noqa: F811
    _stop(device, sink)

    with pytest.raises(LeftTestApp):
        device.apply_setting("window_animation_scale", "0")

    assert sink.calls == []


def test_a_stop_still_lets_the_tracked_settings_be_restored(device, sink):  # noqa: F811
    sink.replies["settings get global window_animation_scale"] = b"1.0\n"
    device.apply_setting("window_animation_scale", "0")
    _stop(device, sink)

    device.restore_settings()

    assert ["settings", "put", "global", "window_animation_scale", "1.0"] in [
        c[-5:] for c in sink.calls
    ]


@pytest.mark.asyncio
async def test_an_inner_layer_that_swallows_the_stop_cannot_send_a_later_command(
    agent, transport, sink, tmp_path
):  # noqa: F811
    request = SimpleNamespace(locked_app_package=PKG, app_path=None, goal="swallow the stop")
    agent.activate_device_policy(SERIAL, run_id="run-9", receipt_dir=tmp_path)

    async def swallow_then_restart(_req):
        sink.foreground = ELSEWHERE
        try:
            agent._device_policy.check_foreground()
        except LeftTestApp:
            pass  # an inner layer that eats the stop
        try:
            transport.run(
                ["-s", SERIAL, "shell", "am", "start", "-n", f"{PKG}/.MainActivity"],
                capture_output=True,
            )
        except LeftTestApp:
            pass
        return "done"

    agent.seen.ran.side_effect = swallow_then_restart

    with pytest.raises(LeftTestApp):
        await agent._run_task(request)

    assert not any("am" in c and "start" in c for c in sink.calls)


@needs_adb
def test_a_stopped_device_serves_no_helper_request_not_even_ping(guest):
    guest.server.devices[SERIAL].services[DEVICE_PORT] = http_service(
        {"/ping": lambda _m, _b: (200, b"{}")}
    )
    guest.device.stopped = True

    with pytest.raises(LeftTestApp):
        helper_stream.request(guest.transport, SERIAL, DEVICE_PORT, "GET", "/ping")

    assert not any(r.request.startswith("tcp:") for r in guest.server.requests)


# ------------------------------- R1: the policy is on before initialization reaches the phone


@needs_adb
@pytest.mark.asyncio
async def test_initialization_sends_the_restricted_phone_nothing_before_the_policy_is_on(
    guest, lease_db, monkeypatch
):
    from artemis.sdk import agent as agent_module
    from artemis.sdk.agent import Agent

    device_policy.reset()  # the lease, not a pre-registered device, must switch the policy on
    guest.log.clear()
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    monkeypatch.setattr(
        agent_module.EndpointTransport, "shared", classmethod(lambda cls, *_a: guest.transport)
    )
    monkeypatch.setattr(agent_module, "create_screen_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(agent_module, "publish_startup_progress", lambda *_a, **_k: None)
    instance = Agent.__new__(Agent)
    instance._initialized = False
    instance._session_id = "run-init"
    instance._tmp_traces_dir = lease_db.path.parent
    instance._prewarm_llm_connections = AsyncMock()
    instance._config = SimpleNamespace(
        device_id=SERIAL,
        device_platform=agent_module.DevicePlatform.ANDROID,
        servers=SimpleNamespace(adb_host="127.0.0.1", adb_port=5037),
    )

    await instance._init_internal()

    assert guest.log == []  # no shell (``wm size`` and the like) reached the phone
    assert instance._device_policy is not None
    assert all(e["decision"] != "allowed" for e in instance._device_policy.audit.entries())


# ------------------- R1: automatic device selection asks a restricted phone nothing either


def _cold_pool(guest):
    from artemis.runtime.device_pool import DevicePool

    guest.server.devices.pop(OTHER, None)  # one phone: automatic selection picks it
    return DevicePool(endpoint=guest.server.endpoint)


@needs_adb
@pytest.mark.asyncio
async def test_automatic_selection_sends_the_restricted_phone_nothing_before_the_policy_is_on(
    guest, lease_db, monkeypatch
):
    import importlib

    from artemis import runtime
    from artemis.sdk import agent as agent_module
    from artemis.sdk.agent import Agent

    device_policy.reset()  # the lease, not a pre-registered device, must switch the policy on
    guest.log.clear()
    guest.server.devices.pop(OTHER)  # one phone: automatic selection picks it
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    monkeypatch.setattr(runtime, "device_pool", _cold_pool(guest))
    monkeypatch.setattr(
        importlib.import_module("artemis.runtime.device_pool"), "identity_observer", None
    )
    monkeypatch.setattr(
        agent_module.EndpointTransport, "shared", classmethod(lambda cls, *_a: guest.transport)
    )
    monkeypatch.setattr(agent_module, "create_screen_client", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(agent_module, "publish_startup_progress", lambda *_a, **_k: None)
    instance = Agent.__new__(Agent)
    instance._initialized = False
    instance._session_id = "run-auto"
    instance._tmp_traces_dir = lease_db.path.parent
    instance._prewarm_llm_connections = AsyncMock()
    instance._config = SimpleNamespace(
        device_id=None,  # automatic selection
        device_platform=None,
        servers=SimpleNamespace(adb_host="127.0.0.1", adb_port=guest.server.endpoint.port),
    )

    await instance._init_internal()

    assert guest.log == []  # not even ``getprop`` reached the phone
    assert not any(r.startswith("shell:") for r in guest.server.request_strings())
    assert instance._device_policy is not None
    assert instance._device_policy.serial == SERIAL


@needs_adb
def test_a_cold_pool_reads_no_properties_of_a_restricted_phone(guest, lease_db):
    device_policy.reset()
    guest.log.clear()
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)

    assert _cold_pool(guest).select_device() == SERIAL

    assert not any(r.startswith("shell:") for r in guest.server.request_strings())


@needs_adb
@pytest.mark.asyncio
async def test_a_cold_async_pool_reads_no_properties_of_a_restricted_phone(guest, lease_db):
    device_policy.reset()
    guest.log.clear()
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)

    assert await _cold_pool(guest).select_device_async() == SERIAL

    assert not any(r.startswith("shell:") for r in guest.server.request_strings())


@needs_adb
def test_an_unreadable_lease_store_means_no_property_read(guest, monkeypatch, tmp_path):
    device_policy.reset()
    guest.log.clear()
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setattr("artemis.runtime.device_policy.lease_db_path", lambda: broken)

    _cold_pool(guest).select_device()

    assert not any(r.startswith("shell:") for r in guest.server.request_strings())


@needs_adb
def test_a_phone_without_a_lease_is_still_described_by_its_properties(guest, lease_db):
    device_policy.reset()  # no restricted lease: discovery is unchanged

    pool = _cold_pool(guest)
    pool.select_device()

    assert any(r.startswith("shell:") for r in guest.server.request_strings())


@needs_adb
@pytest.mark.asyncio
async def test_the_dashboard_probe_asks_a_restricted_phone_nothing(guest, lease_db, monkeypatch):
    from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe

    device_policy.reset()
    guest.log.clear()
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    guest.server.devices.pop(OTHER, None)
    probe = AdbDeviceProbe(endpoint=guest.server.endpoint)
    monkeypatch.setattr(probe, "_transport", lambda _adb: guest.transport)

    listed = await probe._parse_adb_devices(probe._locate_adb())

    assert [d.serial for d in listed] == [SERIAL]
    assert guest.log == []
    assert not any(r.startswith("shell:") for r in guest.server.request_strings())


@needs_adb
@pytest.mark.asyncio
async def test_the_submission_readiness_probe_does_not_query_a_restricted_phone(
    guest, lease_db, monkeypatch
):
    from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe

    device_policy.reset()
    guest.log.clear()
    lease_db.leases.acquire(lease_db.path, ["dev_guest"], "qa", 600, "run", restricted=True)
    guest.server.devices.pop(OTHER, None)
    probe = AdbDeviceProbe(target_serial=SERIAL, endpoint=guest.server.endpoint)
    monkeypatch.setattr(probe, "_transport", lambda _adb: guest.transport)

    result = await probe.probe_submission_readiness(SERIAL)

    assert result.metadata["active_device"] == {"serial": SERIAL, "is_locked": None}  # unknown
    assert guest.log == []
    assert not any(r.startswith("shell:") for r in guest.server.request_strings())


# ---------------------------------------- R4: /action carries only the commands a run needs


def _action_service(guest):
    seen: list[bytes] = []

    def handler(_method, body):
        seen.append(body)
        return 200, b'{"success": true}'

    guest.server.devices[SERIAL].services[DEVICE_PORT] = http_service({"/action": handler})
    return seen


def _post(guest, payload, port=DEVICE_PORT):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return helper_stream.request(guest.transport, SERIAL, port, "POST", "/action", body=body)


@needs_adb
@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": "clipboard", "text": "OVERWRITTEN"},
        {"cmd": "CLIPBOARD", "text": "OVERWRITTEN"},
        {"cmd": "global", "action": "home"},
        {"cmd": "global", "action": "power_dialog"},
        {"cmd": "global", "action": "notifications"},
        {"cmd": "global"},
        {"cmd": "mystery"},
        {"cmd": ["tap"]},
        {},
        [{"cmd": "tap"}],
        b"not json",
        b"",
    ],
    ids=lambda p: str(p)[:40],
)
def test_an_action_that_mutates_the_clipboard_or_the_system_is_denied_before_the_stream(
    guest, payload
):
    seen = _action_service(guest)

    with pytest.raises(DevicePolicyDenied):
        _post(guest, payload)

    assert seen == []
    assert not any(r.request.startswith("tcp:") for r in guest.server.requests)
    assert [e["decision"] for e in guest.audit.entries()] == ["denied"]
    assert "OVERWRITTEN" not in json.dumps(guest.audit.entries())


@needs_adb
@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": "tap", "x": 1, "y": 2},
        {"cmd": "double_tap", "x": 1, "y": 2},
        {"cmd": "long_press", "x": 1, "y": 2},
        {"cmd": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration": 300},
        {"cmd": "type", "text": "hello", "append": True},
        {"cmd": "clear"},
        {"cmd": "global", "action": "back"},
    ],
    ids=lambda p: p["cmd"] + p.get("action", ""),
)
def test_the_input_commands_a_run_needs_still_pass(guest, payload):
    seen = _action_service(guest)

    assert _post(guest, payload) == b'{"success": true}'

    assert [json.loads(b) for b in seen] == [payload]


# ------------------------------------- R4: the helper permission is bound to the helper port


@needs_adb
@pytest.mark.parametrize("path", ["/ping", "/snapshot"])
def test_a_helper_request_to_another_port_is_denied_before_a_device_connection(guest, path):
    reached: list[str] = []
    guest.server.devices[SERIAL].services[OTHER_PORT] = http_service(
        {path: lambda m, _b: (reached.append(m) or 200, b"OTHER-SERVICE-PRIVATE-RESPONSE")}
    )

    with pytest.raises(DevicePolicyDenied):
        helper_stream.request(guest.transport, SERIAL, OTHER_PORT, "GET", path)

    assert reached == []
    assert not any(r.request.startswith("tcp:") for r in guest.server.requests)


@needs_adb
def test_the_helper_stream_connection_takes_no_other_tcp_service(guest):
    _action_service(guest)
    token = device_policy._helper_serial.set(SERIAL)
    try:
        with pytest.raises(DevicePolicyDenied):
            guest.transport.open_stream(SERIAL, OTHER_PORT)
    finally:
        device_policy._helper_serial.reset(token)

    assert not any(r.request == f"tcp:{OTHER_PORT}" for r in guest.server.requests)
