"""Stage 2 of CHE-1363: identity resolution from hashed hardware ids (CHE-1473).

Contract: docs/device-identity.md, "Matching rules" and "Match outcomes".
Seams: host registration over the real agent WebSocket, server adb discovery
through DevicePool, and the browser bridge serial on the server's adb.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3

from fastapi.testclient import TestClient
import pytest

from apps.admin_console.database.repositories.device_repository import DeviceRepository
from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services import device_identity as identity_module
from apps.admin_console.services.device_identity import DeviceIdentity
from apps.admin_console.services.host_admission import host_admission
from apps.admin_console.services.host_registry import host_registry
from artemis.data_engine.storage import StorageManager
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_pool import DevicePool
from tests.unit.admin_console.test_host_device_identity import _connect, _enroll, _sync

SERIALNO = "R5CT1234ABC"
# Shared with host-agent/identity_test.go: the agent and the server must agree.
PEPPER = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
HARDWARE_ID = "359e41333185ec8960b7be84c5ddeee55b34b6805c51b0102f60b5ee5bcc5d38"
PHONE = "sd-05b42e59020251f2"
# The agent's weak id for the same phone before authorization: "dev:adb:" + transport serial.
WEAK = "sd-3a5eede39742af70"
WEAK_WIFI = "sd-b3867fe8155338ce"  # "dev:adb:192.168.1.5:5555"
AVD = "sd-8bcfa99c1405b169"
device_pool_module = importlib.import_module("artemis.runtime.device_pool")
PHONE_PROPS = {"ro.serialno": SERIALNO, "ro.product.model": "Pixel 6 Pro"}


def _avd_props(name: str) -> dict[str, str]:
    return {"ro.serialno": "EMULATOR35X1X1X0", "ro.boot.qemu": "1", "ro.boot.qemu.avd_name": name}


@pytest.fixture
def db(tmp_path, monkeypatch):
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
def repo(db):
    return DeviceRepository(db)


@pytest.fixture
def admin():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000), base_url="http://localhost")


def _local(identity, *devices):
    return identity.observe_adb(AdbEndpoint.local(), list(devices))


def _devices(db) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT device_id, match_state FROM devices ORDER BY rowid").fetchall()


def _host_devices(admin, devices: list[dict], name: str = "Lab Mac", reconnects: int = 1) -> str:
    key, enrolled = _enroll(admin, name)
    for _ in range(reconnects):
        context, ws = _connect(admin, key, enrolled["host_id"])
        try:
            ws.send_json({"type": "devices", "devices": devices})
            _sync(ws)
        finally:
            context.__exit__(None, None, None)
    return enrolled["host_id"]


# -- hashing ------------------------------------------------------------------


def test_the_server_hashes_ro_serialno_like_the_host_agent(identity):
    assert identity.hardware_hash(SERIALNO) == HARDWARE_ID
    for placeholder in ["", "unknown", "0123456789ABCDEF", None]:
        assert identity.hardware_hash(placeholder) is None


def test_the_raw_serial_is_never_stored(db, identity):
    _local(identity, (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS))

    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert HARDWARE_ID in dump
    # On USB the transport serial is ro.serialno: the routing key stores only its keyed hash.
    assert SERIALNO not in dump
    assert identity.transport_key(AdbEndpoint.local(), SERIALNO) in dump


# -- acceptance: one phone, one device ----------------------------------------


def test_usb_and_wifi_of_one_phone_resolve_to_one_device(db, identity, repo):
    usb, wifi = _local(
        identity,
        (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS),
        ("192.168.1.5:5555", "device", "Pixel 6 Pro", PHONE_PROPS),
    )

    assert usb.device_id == wifi.device_id
    assert usb.connection_id != wifi.connection_id
    assert (usb.outcome, wifi.outcome) == ("confirmed", "confirmed")
    assert _devices(db) == [(usb.device_id, "confirmed")]
    wifi_key = identity.transport_key(AdbEndpoint.local(), "192.168.1.5:5555")
    assert repo.device_for_connection("local", None, wifi_key) == usb.device_id


def test_host_agent_usb_and_server_wifi_of_one_phone_resolve_to_one_device(db, admin, identity):
    host_id = _host_devices(
        admin,
        [
            {
                "serial": PHONE,
                "state": "device",
                "kind": "physical",
                "shared": True,
                "hardware_id": HARDWARE_ID,
            }
        ],
    )
    (wifi,) = _local(identity, ("192.168.1.5:5555", "device", "Pixel 6 Pro", PHONE_PROPS))

    assert _devices(db) == [(wifi.device_id, "confirmed")]
    repo = DeviceRepository(db)
    assert repo.device_for_connection("host", host_id, PHONE) == wifi.device_id


def test_a_browser_bridge_phone_joins_its_usb_device(db, identity, monkeypatch):
    monkeypatch.setattr(identity_module, "_bridge_serials", lambda: {"127.0.0.1:40123"})
    usb, bridged = _local(
        identity,
        (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS),
        ("127.0.0.1:40123", "device", "Pixel 6 Pro", PHONE_PROPS),
    )

    assert bridged.device_id == usb.device_id
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT kind, source FROM device_connections WHERE connection_id = ?",
            (bridged.connection_id,),
        ).fetchone() == ("bridge", "bridge")


# -- acceptance: old host agent and AVD recycle -------------------------------


def test_an_old_host_agent_reconnecting_creates_no_new_device(db, admin, repo):
    old = [{"serial": PHONE, "state": "device", "kind": "physical", "shared": True}]
    host_id = _host_devices(admin, old, reconnects=4)

    ((device_id, state),) = _devices(db)
    assert state == "uncertain"  # no hashed id: never confirmed, never a second device
    assert repo.device_for_connection("host", host_id, PHONE) == device_id


def test_an_old_host_agent_is_not_merged_into_a_known_phone(db, admin, identity):
    (usb,) = _local(identity, (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS))
    _host_devices(admin, [{"serial": PHONE, "state": "device", "kind": "physical"}])

    assert _devices(db)[0] == (usb.device_id, "confirmed")
    assert _devices(db)[1][1] == "uncertain"  # the owner decides (Merge, CHE-1480)


def test_an_updated_host_agent_confirms_its_uncertain_record(db, admin, identity):
    old = {"serial": PHONE, "state": "device", "kind": "physical", "shared": True}
    host_id = _host_devices(admin, [old])
    ((before, _),) = _devices(db)

    identity.observe_host(host_id, [{**old, "hardware_id": HARDWARE_ID}])

    assert _devices(db) == [(before, "confirmed")]


def test_an_avd_recycle_is_one_device_and_instances_are_connections(db, identity):
    (first,) = _local(identity, ("emulator-5554", "device", "sdk", _avd_props("Pixel_A")))
    (second,) = _local(identity, ("emulator-5556", "device", "sdk", _avd_props("Pixel_A")))
    (other,) = _local(identity, ("emulator-5554", "device", "sdk", _avd_props("Pixel_B")))

    assert second.device_id == first.device_id
    assert second.connection_id != first.connection_id
    assert other.device_id != first.device_id  # a different AVD on a reused instance serial
    with sqlite3.connect(db) as conn:
        kinds = conn.execute("SELECT DISTINCT kind FROM device_connections").fetchall()
    assert kinds == [("avd",)]


def test_a_host_agent_avd_is_one_device_across_reconnects(db, admin):
    avd = {"serial": AVD, "state": "device", "kind": "emulator", "shared": True}
    _host_devices(admin, [avd], reconnects=3)

    ((_, state),) = _devices(db)
    assert state == "confirmed"


# -- acceptance: provisional --------------------------------------------------


def test_provisional_becomes_confirmed_once_the_identity_is_read(db, identity):
    (unauthorized,) = _local(identity, ("192.168.1.5:5555", "unauthorized", None, {}))
    assert unauthorized.outcome == "provisional"
    (offline,) = _local(identity, ("192.168.1.5:5555", "offline", None, {}))
    assert offline == unauthorized

    (read,) = _local(identity, ("192.168.1.5:5555", "device", "Pixel 6 Pro", PHONE_PROPS))

    assert (read.device_id, read.connection_id) == (
        unauthorized.device_id,
        unauthorized.connection_id,
    )
    assert read.outcome == "confirmed"
    assert _devices(db) == [(read.device_id, "confirmed")]


def test_a_provisional_record_joins_the_phone_it_turns_out_to_be(db, identity, repo):
    (usb,) = _local(identity, (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS))
    (pending,) = _local(identity, ("192.168.1.5:5555", "unauthorized", None, {}))
    assert pending.device_id != usb.device_id

    (read,) = _local(identity, ("192.168.1.5:5555", "device", "Pixel 6 Pro", PHONE_PROPS))

    assert (read.device_id, read.outcome) == (usb.device_id, "confirmed")
    assert read.connection_id == pending.connection_id
    assert _devices(db) == [(usb.device_id, "confirmed")]
    assert repo.resolve(pending.device_id) == usb.device_id  # the old id stays resolvable


def test_an_unauthorized_host_device_is_provisional(db, admin):
    _host_devices(admin, [{"serial": PHONE, "state": "unauthorized", "kind": "physical"}])

    assert [state for _, state in _devices(db)] == ["provisional"]


def test_a_placeholder_serial_is_uncertain(db, identity):
    (match,) = _local(identity, ("ZY22", "device", "Moto", {"ro.serialno": "0123456789ABCDEF"}))

    assert match.outcome == "uncertain"


def test_a_malformed_hardware_id_is_ignored(db, admin):
    _host_devices(
        admin,
        [{"serial": PHONE, "state": "device", "kind": "physical", "hardware_id": SERIALNO}],
    )

    assert [state for _, state in _devices(db)] == ["uncertain"]
    assert SERIALNO not in "\n".join(sqlite3.connect(db).iterdump())


def test_another_phone_on_a_recycled_transport_key_gets_its_own_device(db, identity, repo):
    (first,) = _local(identity, ("192.168.1.5:5555", "device", "Pixel 6 Pro", PHONE_PROPS))
    other = {"ro.serialno": "OTHERPHONE1"}

    (second,) = _local(identity, ("192.168.1.5:5555", "device", "Pixel 3a", other))

    assert second.device_id != first.device_id
    assert second.connection_id != first.connection_id  # the old connection keeps its history
    wifi_key = identity.transport_key(AdbEndpoint.local(), "192.168.1.5:5555")
    assert repo.device_for_connection("local", None, wifi_key) == second.device_id
    assert repo.get(first.device_id).match_state == "confirmed"


def test_host_authorization_promotes_the_provisional_device(db, admin, repo):
    # The agent names an unauthorized phone by its transport (weak id); once authorized it
    # switches to the hardware id and lists the weak ids it replaced.
    key, enrolled = _enroll(admin, "Lab Mac")
    host_id = enrolled["host_id"]
    context, ws = _connect(admin, key, host_id)
    try:
        ws.send_json(
            {
                "type": "devices",
                "devices": [{"serial": WEAK, "state": "unauthorized", "kind": "physical"}],
            }
        )
        _sync(ws)
        ((pending, state),) = _devices(db)
        assert state == "provisional"
        trusted = {
            "serial": PHONE,
            "state": "device",
            "kind": "physical",
            "shared": True,
            "hardware_id": HARDWARE_ID,
            "previous_ids": [WEAK],
        }
        ws.send_json({"type": "devices", "devices": [trusted]})
        _sync(ws)
    finally:
        context.__exit__(None, None, None)

    assert _devices(db) == [(pending, "confirmed")]
    assert repo.device_for_connection("host", host_id, PHONE) == pending
    assert repo.device_for_connection("host", host_id, WEAK) is None


def _report(ws, *devices: dict) -> None:
    ws.send_json({"type": "devices", "devices": list(devices)})
    _sync(ws)


def _weak(serial: str) -> dict:
    return {"serial": serial, "state": "unauthorized", "kind": "physical"}


def _trusted(*previous: str) -> dict:
    return {
        "serial": PHONE,
        "state": "device",
        "kind": "physical",
        "shared": True,
        "hardware_id": HARDWARE_ID,
        "previous_ids": list(previous),
    }


def test_usb_and_wifi_authorized_together_become_one_device(db, admin, repo):
    key, enrolled = _enroll(admin, "Lab Mac")
    host_id = enrolled["host_id"]
    context, ws = _connect(admin, key, host_id)
    try:
        _report(ws, _weak(WEAK), _weak(WEAK_WIFI))
        usb_pending, wifi_pending = (device_id for device_id, _ in _devices(db))
        _report(ws, _trusted(WEAK, WEAK_WIFI))
    finally:
        context.__exit__(None, None, None)

    assert _devices(db) == [(usb_pending, "confirmed")]
    assert repo.resolve(wifi_pending) == usb_pending  # the old id stays resolvable
    with sqlite3.connect(db) as conn:  # both connections, and their history, stay
        assert conn.execute(
            "SELECT COUNT(*) FROM device_connections WHERE device_id = ?", (usb_pending,)
        ).fetchone() == (2,)


def test_wifi_authorized_after_usb_joins_the_confirmed_phone(db, admin, repo):
    key, enrolled = _enroll(admin, "Lab Mac")
    host_id = enrolled["host_id"]
    context, ws = _connect(admin, key, host_id)
    try:
        _report(ws, _trusted(WEAK))
        ((phone, _),) = _devices(db)
        _report(ws, _trusted(WEAK), _weak(WEAK_WIFI))
        wifi_pending = _devices(db)[1][0]
        _report(ws, _trusted(WEAK, WEAK_WIFI))
    finally:
        context.__exit__(None, None, None)

    assert _devices(db) == [(phone, "confirmed")]
    assert repo.resolve(wifi_pending) == phone
    assert repo.device_for_connection("host", host_id, WEAK_WIFI) == phone


def test_previous_ids_never_take_a_confirmed_foreign_or_other_host_record(
    db, admin, identity, repo
):
    host_id = _host_devices(admin, [_weak(WEAK_WIFI)])
    with sqlite3.connect(db) as conn:  # the Wi-Fi record belongs to someone else
        conn.execute("UPDATE devices SET owner_principal_id = 'p-bob'")
    ((foreign, _),) = _devices(db)
    other = identity.hardware_hash("OTHERPHONE1")
    identity.observe_host(host_id, [{**_trusted(), "serial": WEAK, "hardware_id": other}])
    confirmed = repo.device_for_connection("host", host_id, WEAK)
    elsewhere = _host_devices(admin, [_weak(WEAK)], name="Desk PC")
    remote = repo.device_for_connection("host", elsewhere, WEAK)

    identity.observe_host(host_id, [_trusted()])
    phone = repo.device_for_connection("host", host_id, PHONE)

    identity.observe_host(host_id, [_trusted(WEAK, WEAK_WIFI)])

    assert repo.device_for_connection("host", host_id, PHONE) == phone
    assert phone not in (foreign, confirmed, remote)
    states = dict(_devices(db))
    assert states[foreign] == "provisional"  # owners differ: the owner decides (Merge)
    assert repo.get(confirmed).hardware_hash == other  # a confirmed identity stays put
    assert states[remote] == "provisional"
    assert repo.resolve(remote) == remote  # another host's record is never touched


def test_host_authorization_joins_a_phone_the_server_already_knows(db, admin, identity, repo):
    (usb,) = _local(identity, (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS))
    host_id = _host_devices(admin, [{"serial": WEAK, "state": "unauthorized", "kind": "physical"}])
    weak = {"serial": PHONE, "state": "device", "kind": "physical", "hardware_id": HARDWARE_ID}

    identity.observe_host(host_id, [{**weak, "previous_ids": [WEAK]}])

    assert _devices(db) == [(usb.device_id, "confirmed")]
    assert repo.device_for_connection("host", host_id, PHONE) == usb.device_id


def test_two_adb_servers_keep_their_avds_and_serials_apart(db, identity, repo):
    lab = AdbEndpoint.create("10.0.0.2", 5037)
    here = identity.observe_adb(
        AdbEndpoint.local(), [("emulator-5554", "device", "sdk", _avd_props("Pixel_A"))]
    )
    there = identity.observe_adb(lab, [("emulator-5554", "device", "sdk", _avd_props("Pixel_A"))])
    assert here[0].device_id != there[0].device_id

    other = {"ro.serialno": "OTHERPHONE1"}
    for _ in range(3):
        identity.observe_adb(
            AdbEndpoint.local(), [("192.168.1.5:5555", "device", "P6", PHONE_PROPS)]
        )
        identity.observe_adb(lab, [("192.168.1.5:5555", "device", "P3a", other)])

    with sqlite3.connect(db) as conn:
        (wifi,) = conn.execute(
            "SELECT COUNT(*) FROM device_connections WHERE kind = 'wifi'"
        ).fetchone()
    assert wifi == 2
    assert len(_devices(db)) == 4


def _pool(monkeypatch, identity, listings: list[list[tuple]], reads: dict[str, list[dict]]):
    """A pool whose fresh enumerations return ``listings`` in turn and whose
    property reads pop from ``reads[serial]``; the observer is the real one."""
    pool = DevicePool()
    monkeypatch.setattr(pool, "_resolve_adb", lambda: "adb")
    monkeypatch.setattr(pool, "_query_adb_devices_sync", lambda: listings.pop(0))
    monkeypatch.setattr(pool, "_read_properties_sync", lambda serial: reads[serial].pop(0))
    monkeypatch.setattr(device_pool_module, "identity_observer", identity.observe_adb)

    def enumerate_all() -> None:
        while listings:
            pool._snapshot().raw = None  # force a fresh enumeration
            pool.list_devices()

    return enumerate_all


def test_another_phone_at_the_same_wifi_address_is_read_again(db, identity, monkeypatch):
    wifi, usb = "192.168.1.5:5555", "ZY22USB1"
    other = {"ro.serialno": "OTHERPHONE1", "ro.product.model": "Pixel 3a"}
    usb_props = {"ro.serialno": "ZY22USB1", "ro.product.model": "Moto"}
    listing = [(wifi, "device", "P", None), (usb, "device", "Moto", None)]
    reads = {wifi: [dict(PHONE_PROPS), other], usb: [usb_props]}
    _pool(monkeypatch, identity, [listing, list(listing)], reads)()

    assert reads == {wifi: [], usb: []}  # Wi-Fi identity re-read; the USB phone read once
    assert [state for _, state in _devices(db)] == ["confirmed", "confirmed", "confirmed"]
    with sqlite3.connect(db) as conn:
        hashes = {row[0] for row in conn.execute("SELECT hardware_hash FROM devices")}
    assert hashes == {
        HARDWARE_ID,
        identity.hardware_hash("OTHERPHONE1"),
        identity.hardware_hash(usb),
    }


def test_the_async_listing_reads_a_reused_wifi_address_again(db, identity, monkeypatch):
    wifi = "192.168.1.5:5555"
    pool = DevicePool()
    monkeypatch.setattr(pool, "_resolve_adb", lambda: "adb")

    async def query():
        return [(wifi, "device", "P", None)]

    reads = [dict(PHONE_PROPS), {"ro.serialno": "OTHERPHONE1"}]

    async def read(serial):
        return reads.pop(0)

    monkeypatch.setattr(pool, "_query_adb_devices_async", query)
    monkeypatch.setattr(pool, "_read_properties_async", read)
    monkeypatch.setattr(device_pool_module, "identity_observer", identity.observe_adb)

    async def twice():
        for _ in range(2):
            pool._snapshot().raw = None
            await pool.list_devices_async()

    asyncio.run(twice())

    assert reads == []
    assert len(_devices(db)) == 2


def test_another_avd_after_an_offline_listing_is_read_again(db, identity, monkeypatch):
    listings = [
        [("emulator-5554", "device", "sdk", None)],
        [("emulator-5554", "offline", None, None)],
        [("emulator-5554", "device", "sdk", None)],
    ]
    reads = {"emulator-5554": [_avd_props("Pixel_A"), _avd_props("Pixel_B")]}
    _pool(monkeypatch, identity, listings, reads)()

    assert reads == {"emulator-5554": []}
    assert len(_devices(db)) == 2  # Pixel_B is not taken for Pixel_A


def test_a_phone_that_leaves_and_returns_is_read_again(db, identity, monkeypatch):
    # Control: an absent listing already forgets the transport.
    wifi = "192.168.1.5:5555"
    other = {"ro.serialno": "OTHERPHONE1"}
    listings = [[(wifi, "device", "P", None)], [], [(wifi, "device", "P", None)]]
    _pool(monkeypatch, identity, listings, {wifi: [dict(PHONE_PROPS), other]})()

    assert len(_devices(db)) == 2


def test_a_missing_ro_serialno_is_read_again(monkeypatch):
    pool = DevicePool()
    monkeypatch.setattr(pool, "_resolve_adb", lambda: "adb")
    monkeypatch.setattr(pool, "UNKNOWN_KIND_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(
        pool, "_query_adb_devices_sync", lambda: [(SERIALNO, "device", "Pixel 6 Pro", "raven")]
    )
    reads = [{"ro.product.model": "Pixel 6 Pro"}, dict(PHONE_PROPS)]
    monkeypatch.setattr(pool, "_read_properties_sync", lambda serial: reads.pop(0))
    seen = []
    monkeypatch.setattr(device_pool_module, "identity_observer", lambda _e, d: seen.append(d))

    pool.list_devices()
    pool._snapshot().raw = None  # force a fresh enumeration
    pool.list_devices()

    assert reads == []
    assert seen[-1] == [(SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS)]


# -- hooks never break their source -------------------------------------------


def test_discovery_feeds_the_observer_and_survives_a_failing_store(db, identity, monkeypatch):
    pool = DevicePool()
    monkeypatch.setattr(pool, "_resolve_adb", lambda: "adb")
    monkeypatch.setattr(
        pool,
        "_query_adb_devices_sync",
        lambda: [
            (SERIALNO, "device", "Pixel 6 Pro", "raven"),
            ("10.0.0.9:5555", "offline", None, None),
        ],
    )
    monkeypatch.setattr(pool, "_read_properties_sync", lambda serial: dict(PHONE_PROPS))
    seen = []

    def observe(endpoint, devices):
        seen.append(devices)
        return identity.observe_adb(endpoint, devices)

    monkeypatch.setattr(device_pool_module, "identity_observer", observe)

    pool.list_devices()

    assert seen == [
        [
            (SERIALNO, "device", "Pixel 6 Pro", PHONE_PROPS),
            ("10.0.0.9:5555", "offline", None, {}),
        ]
    ]
    assert [state for _, state in _devices(db)] == ["confirmed", "provisional"]

    def broken(self, **_observed):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DeviceRepository, "match_connection", broken)
    monkeypatch.setattr(identity, "_seen", {})
    pool._snapshot().raw = None  # force a fresh enumeration
    assert [d.serial for d in pool.list_devices()] == [SERIALNO, "10.0.0.9:5555"]


def test_host_registration_survives_a_failing_device_store(db, admin, identity, monkeypatch):
    def broken(self, **_observed):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DeviceRepository, "match_connection", broken)
    phone = {"serial": PHONE, "state": "device", "kind": "physical", "shared": True}

    _host_devices(admin, [phone])

    assert [d["serial"] for d in admin.get("/api/hosts").json()["devices"]] == [PHONE]
    assert _devices(db) == []
