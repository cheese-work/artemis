"""CHE-1477 correction (P1-1): production sources feed the reconciler and a lifecycle tick expires TTL.

Entry points under test: server adb discovery (``DeviceIdentity.observe_adb``), host
registration over the real agent WebSocket, and the server's startup/shutdown hooks.
No device is touched; time is a fake server clock.
"""

import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from apps.admin_console import server
from apps.admin_console.core.state import state
from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services import device_identity as identity_module
from apps.admin_console.services import device_reconciler as reconciler_module
from apps.admin_console.services.device_identity import DeviceIdentity
from apps.admin_console.services.host_admission import host_admission
from apps.admin_console.services.host_registry import host_registry
from artemis.data_engine.storage import StorageManager
from artemis.runtime.adb_endpoint import AdbEndpoint
from tests.unit.admin_console.test_device_identity_resolution import (
    HARDWARE_ID,
    PEPPER,
    PHONE,
    PHONE_PROPS,
    SERIALNO,
)
from tests.unit.admin_console.test_host_device_identity import _connect, _enroll, _sync
from tests.unit.admin_console.test_preview_profile import _clean_state, _stub_hooks  # noqa: F401

WIFI = "192.168.1.5:5555"
START = 1_000.0


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    # raising=False: the seam is part of the fix, so the tests reach their assertions first.
    monkeypatch.setattr(reconciler_module, "server_time", fake, raising=False)
    return fake


@pytest.fixture
def db(tmp_path, monkeypatch, clock):
    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "enabled")
    monkeypatch.setattr(host_registry, "db_path", path)
    monkeypatch.setattr(host_registry, "clock", lambda: 1_800_000_000.0)
    host_registry.reset_memory()
    host_admission.reset()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS host_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO host_settings VALUES ('device_pepper', ?)", (PEPPER,))
    yield path
    host_registry.reset_memory()
    host_admission.reset()


@pytest.fixture
def identity(db, monkeypatch):
    fresh = DeviceIdentity()
    monkeypatch.setattr(identity_module, "device_identity", fresh)
    return fresh


@pytest.fixture
def admin():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000), base_url="http://localhost")


def _events(db) -> list[tuple[str, str, float]]:
    """``(device_id, state, ts)`` of every state event, oldest first."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT device_id, payload, ts FROM device_events WHERE kind = 'state_changed' "
            "ORDER BY event_id"
        ).fetchall()
    return [(device_id, json.loads(payload)["state"], ts) for device_id, payload, ts in rows]


def _adb(identity, *devices):
    return identity.observe_adb(AdbEndpoint.local(), list(devices))


def _phone(state="device", serial=SERIALNO):
    return (serial, state, "Pixel 6 Pro", PHONE_PROPS)


def _host(admin, devices):
    key, enrolled = _enroll(admin, "Lab Mac")
    context, ws = _connect(admin, key, enrolled["host_id"])
    try:
        ws.send_json({"type": "devices", "devices": devices})
        _sync(ws)
    finally:
        context.__exit__(None, None, None)
    return enrolled["host_id"]


# -- sources ------------------------------------------------------------------


def test_server_adb_discovery_feeds_the_reconciler(db, clock, identity):
    (match,) = _adb(identity, _phone())
    clock.now += 6
    _adb(identity, _phone())

    assert _events(db) == [(match.device_id, "idle", START)]


def test_repeated_discovery_of_one_phone_adds_no_event(db, clock, identity):
    _adb(identity, _phone())
    for _ in range(30):
        clock.now += 1
        _adb(identity, _phone())

    assert [state for _, state, _ in _events(db)] == ["idle"]


def test_an_adb_state_other_than_device_reports_disconnected(db, clock, identity):
    (match,) = _adb(identity, _phone("offline"))
    clock.now += 6
    _adb(identity, _phone("offline"))

    assert _events(db) == [(match.device_id, "disconnected", START)]


def test_one_phone_on_usb_and_wifi_has_one_state_stream(db, clock, identity):
    (usb,) = _adb(identity, _phone())
    clock.now += 6
    (wifi,) = _adb(identity, _phone(serial=WIFI))
    clock.now += 6
    _adb(identity, _phone(), _phone(serial=WIFI))

    assert usb.device_id == wifi.device_id
    assert {device_id for device_id, _, _ in _events(db)} == {usb.device_id}
    assert [state for _, state, _ in _events(db)] == ["idle"]


def test_host_registration_feeds_the_reconciler_with_server_time(db, clock, admin, identity):
    devices = [
        {
            "serial": PHONE,
            "state": "device",
            "kind": "physical",
            "shared": True,
            "hardware_id": HARDWARE_ID,
            "ts": 5.0,  # a sender's clock is ignored
        }
    ]
    _host(admin, devices)
    clock.now += 6
    _host(admin, devices)

    ((device_id, state, ts),) = _events(db)
    assert (state, ts) == ("idle", START)


# -- lifecycle ----------------------------------------------------------------


async def _until(predicate, timeout=3.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_startup_registers_a_tick_that_turns_a_silent_host_unknown(
    db, clock, identity, monkeypatch
):
    monkeypatch.setattr(reconciler_module, "TICK_S", 0.01, raising=False)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", False)
    _stub_hooks(monkeypatch, forbidden=False)
    (match,) = _adb(identity, _phone())
    clock.now += 6
    _adb(identity, _phone())  # the last heartbeat: START + 6, then silence

    await server.on_startup()
    try:
        clock.now = START + 6 + 75  # past the 60 s TTL and the 5 s hold; nobody reports
        assert await _until(lambda: [s for _, s, _ in _events(db)] == ["idle", "unknown"])
    finally:
        await server.on_shutdown()

    assert _events(db)[-1] == (match.device_id, "unknown", START + 6 + 60)


@pytest.mark.asyncio
async def test_shutdown_stops_the_tick_and_leaves_no_task(db, clock, identity, monkeypatch):
    monkeypatch.setattr(reconciler_module, "TICK_S", 0.01, raising=False)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", False)
    _stub_hooks(monkeypatch, forbidden=False)

    await server.on_startup()
    task = getattr(state, "reconciler_task", None)
    assert task is not None and not task.done()

    await server.on_shutdown()

    assert task.done()
    assert state.reconciler_task is None


@pytest.mark.asyncio
async def test_a_failing_tick_is_logged_and_the_loop_keeps_running(
    db, clock, identity, monkeypatch
):
    monkeypatch.setattr(reconciler_module, "TICK_S", 0.01, raising=False)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", False)
    _stub_hooks(monkeypatch, forbidden=False)
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reconciler_module.DeviceReconciler, "tick", flaky)
    _adb(identity, _phone())  # creates the reconciler for this database

    await server.on_startup()
    try:
        assert await _until(lambda: len(calls) >= 3)
    finally:
        await server.on_shutdown()
