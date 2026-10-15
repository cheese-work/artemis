"""Stage 4 of CHE-1363: the device state reconciler (CHE-1477).

Contract: docs/device-identity.md ("Device state"), docs/board-operations.md (config keys).
"""

import json
import sqlite3

import pytest

from artemis.data_engine.storage import StorageManager

DEVICE = "dev_1"
USB = "con_usb"
WIFI = "con_wifi"


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    return path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(db, clock):
    # Imports stay inside the factory so a missing module fails each test, not collection.
    def build(**kwargs):
        from apps.admin_console.database.repositories.device_event_repository import (
            DeviceEventRepository,
        )
        from apps.admin_console.services.device_reconciler import DeviceReconciler

        return DeviceReconciler(DeviceEventRepository(db), clock=clock, ttl_s=60, **kwargs)

    return build


def _events(db, device_id=DEVICE) -> list[tuple[str, float]]:
    """``(state, ts)`` of every state event, oldest first."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT payload, ts FROM device_events WHERE device_id = ? AND kind = 'state_changed' "
            "ORDER BY event_id",
            (device_id,),
        ).fetchall()
    return [(json.loads(payload)["state"], ts) for payload, ts in rows]


def _states(db, device_id=DEVICE) -> list[str]:
    return [state for state, _ in _events(db, device_id)]


def _beat(rec, state="idle", connection=USB, device=DEVICE):
    rec.heartbeat(device, connection, state)
    rec.tick()


# -- config -------------------------------------------------------------------


def test_device_ttl_defaults_to_sixty_seconds(monkeypatch):
    from apps.admin_console.services.device_reconciler import device_ttl_s

    monkeypatch.delenv("ARTEMIS_DEVICE_TTL_S", raising=False)
    assert device_ttl_s() == 60


def test_device_ttl_reads_the_environment(monkeypatch):
    from apps.admin_console.services.device_reconciler import device_ttl_s

    monkeypatch.setenv("ARTEMIS_DEVICE_TTL_S", "120")
    assert device_ttl_s() == 120


@pytest.mark.parametrize("raw", ["abc", "9", "3601", "-5", "nan", ""])
def test_device_ttl_falls_back_when_missing_or_out_of_range(monkeypatch, raw):
    from apps.admin_console.services.device_reconciler import device_ttl_s

    monkeypatch.setenv("ARTEMIS_DEVICE_TTL_S", raw)
    assert device_ttl_s() == 60


# -- hysteresis and idempotence -----------------------------------------------


def test_a_new_state_is_written_only_after_it_holds_for_five_seconds(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(4.9)
    _beat(rec)
    assert _states(db) == []

    clock.advance(0.1)
    _beat(rec)

    assert _states(db) == ["idle"]
    assert rec.state(DEVICE) == "idle"


def test_the_event_is_stamped_when_the_state_first_appeared(db, clock, make):
    rec = make()
    started = clock.now
    _beat(rec)
    clock.advance(7)
    _beat(rec)

    assert _events(db) == [("idle", started)]


def test_replaying_a_heartbeat_adds_no_event(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    for _ in range(20):
        _beat(rec)
        clock.advance(1)

    assert _states(db) == ["idle"]


def test_wifi_flapping_emits_a_bounded_number_of_events(db, clock, make):
    rec = make()
    _beat(rec, "idle", WIFI)
    clock.advance(5)
    _beat(rec, "idle", WIFI)
    for second in range(120):  # drops and returns every second for two minutes
        clock.advance(1)
        _beat(rec, "disconnected" if second % 2 == 0 else "idle", WIFI)

    assert _states(db) == ["idle"]


def test_a_state_that_holds_after_flapping_is_written_once(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    for second in range(10):
        clock.advance(1)
        _beat(rec, "disconnected" if second % 2 == 0 else "idle")
    for _ in range(8):
        clock.advance(1)
        _beat(rec, "disconnected")

    assert _states(db) == ["idle", "disconnected"]


def test_the_same_transition_twice_writes_one_row(db, clock, make):
    first, second = make(), make()  # two reconcilers on one database, e.g. a restart overlap
    for rec in (first, second):
        _beat(rec)
    clock.advance(5)
    for rec in (first, second):
        _beat(rec)

    assert _states(db) == ["idle"]


# -- TTL ----------------------------------------------------------------------


def test_a_host_that_stops_heartbeating_shows_unknown_after_the_ttl(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    silent_since = clock.now

    clock.advance(59)
    rec.tick()
    assert rec.state(DEVICE) == "idle"

    clock.advance(1)  # TTL reached
    rec.tick()
    clock.advance(5)  # and held
    rec.tick()

    assert rec.state(DEVICE) == "unknown"
    assert _states(db) == ["idle", "unknown"]
    assert _events(db)[-1][1] == silent_since + 60  # stamped when the TTL ran out


def test_unknown_is_never_idle_for_a_device_that_was_never_heard_from(make):
    assert make().state(DEVICE) == "unknown"


def test_a_returning_heartbeat_leaves_unknown_after_the_hold(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    clock.advance(70)
    rec.tick()
    assert rec.state(DEVICE) == "unknown"

    _beat(rec)
    assert rec.state(DEVICE) == "unknown"
    clock.advance(5)
    _beat(rec)

    assert rec.state(DEVICE) == "idle"
    assert _states(db) == ["idle", "unknown", "idle"]


# -- connections --------------------------------------------------------------


def test_one_phone_on_usb_and_wifi_is_one_device_state(db, clock, make):
    rec = make()
    _beat(rec, "idle", USB)
    _beat(rec, "disconnected", WIFI)
    clock.advance(5)
    _beat(rec, "idle", USB)
    _beat(rec, "disconnected", WIFI)

    assert rec.state(DEVICE) == "idle"  # one live connection is enough
    assert _states(db) == ["idle"]


def test_a_silent_connection_does_not_count(db, clock, make):
    rec = make()
    _beat(rec, "running", USB)
    clock.advance(30)
    _beat(rec, "idle", WIFI)
    clock.advance(40)  # USB is 70 s silent, Wi-Fi 40 s
    _beat(rec, "idle", WIFI)
    clock.advance(5)
    _beat(rec, "idle", WIFI)

    assert rec.state(DEVICE) == "idle"


def test_a_device_is_disconnected_when_every_live_connection_says_so(db, clock, make):
    rec = make()
    _beat(rec, "disconnected", USB)
    _beat(rec, "disconnected", WIFI)
    clock.advance(5)
    _beat(rec, "disconnected", USB)

    assert rec.state(DEVICE) == "disconnected"


def test_devices_do_not_share_state(db, clock, make):
    rec = make()
    _beat(rec, "idle", USB, "dev_a")
    _beat(rec, "running", WIFI, "dev_b")
    clock.advance(5)
    _beat(rec, "idle", USB, "dev_a")
    _beat(rec, "running", WIFI, "dev_b")

    assert (_states(db, "dev_a"), _states(db, "dev_b")) == (["idle"], ["running"])


def test_an_unknown_state_name_is_refused(make):
    with pytest.raises(ValueError):
        make().heartbeat(DEVICE, USB, "sleeping")


# -- restart ------------------------------------------------------------------


def test_a_restart_does_not_repeat_the_last_known_state(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)

    again = make()  # fresh process, same database
    _beat(again)
    clock.advance(5)
    _beat(again)

    assert _states(db) == ["idle"]


def test_startup_writes_unknown_for_devices_last_seen_in_a_known_state(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    clock.advance(3600)  # server down for an hour

    again = make()
    again.start()

    assert _states(db) == ["idle", "unknown"]
    assert _events(db)[-1][1] == clock.now
    assert again.state(DEVICE) == "unknown"


def test_startup_twice_writes_one_unknown(db, clock, make):
    rec = make()
    _beat(rec)
    clock.advance(5)
    _beat(rec)
    clock.advance(3600)

    make().start()
    make().start()

    assert _states(db) == ["idle", "unknown"]
