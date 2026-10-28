"""Device leases: one admission authority for exclusive use of a phone (CHE-2099, S3.2)."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from apps.admin_console.services import device_leases as leases
from artemis.data_engine.storage import StorageManager

NOW = 1_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    return path


@pytest.fixture
def clock(monkeypatch):
    now = [NOW]
    monkeypatch.setattr(leases, "clock", lambda: now[0])
    return now


def take(db, device="dev_a", holder="agent-1", ttl=60, **kwargs):
    return leases.acquire(db, [device], holder, ttl, kwargs.pop("purpose", "qa"), **kwargs)


def test_a_second_agent_is_refused_and_told_who_holds_the_phone(db, clock):
    first = take(db, holder="agent-1")

    with pytest.raises(leases.DeviceBusy) as busy:
        take(db, holder="agent-2")

    assert busy.value.holder.holder == "agent-1"
    assert busy.value.holder.lease_id == first.lease_id
    assert busy.value.holder.expires_at == NOW + 60


def test_two_racing_agents_exactly_one_wins(db, clock):
    start = threading.Barrier(8)

    def race(n):
        start.wait()
        try:
            return take(db, holder=f"agent-{n}")
        except leases.DeviceBusy:
            return None

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(race, range(8)))

    assert len([r for r in results if r is not None]) == 1
    assert len(leases.active(db)) == 1


def test_expiry_frees_the_phone(db, clock):
    take(db, holder="agent-1", ttl=60)
    clock[0] = NOW + 59
    with pytest.raises(leases.DeviceBusy):
        take(db, holder="agent-2")

    clock[0] = NOW + 60

    assert leases.active(db) == []
    assert take(db, holder="agent-2").holder == "agent-2"


def test_renew_moves_the_expiry_for_the_holder_only(db, clock):
    lease = take(db, ttl=60)
    clock[0] = NOW + 30

    renewed = leases.renew(db, lease.lease_id, "agent-1", 60)

    assert renewed.expires_at == NOW + 90
    with pytest.raises(leases.LeaseError) as other:
        leases.renew(db, lease.lease_id, "agent-2", 60)
    assert other.value.code == "lease_not_yours"


def test_an_expired_lease_cannot_be_renewed_or_released(db, clock):
    lease = take(db, ttl=60)
    clock[0] = NOW + 61

    with pytest.raises(leases.LeaseError) as renew:
        leases.renew(db, lease.lease_id, "agent-1", 60)
    with pytest.raises(leases.LeaseError) as release:
        leases.release(db, lease.lease_id, "agent-1")

    assert renew.value.code == "lease_expired"
    assert release.value.code == "lease_unknown"


def test_release_frees_the_phone_and_only_for_the_holder(db, clock):
    lease = take(db)
    with pytest.raises(leases.LeaseError) as other:
        leases.release(db, lease.lease_id, "agent-2")
    assert other.value.code == "lease_not_yours"

    leases.release(db, lease.lease_id, "agent-1")

    assert take(db, holder="agent-2").holder == "agent-2"


def test_an_admin_may_release_and_renew_any_lease(db, clock):
    lease = take(db)

    leases.renew(db, lease.lease_id, "admin", 120, admin=True)
    leases.release(db, lease.lease_id, "admin", admin=True)

    assert leases.active(db) == []


def test_a_pool_takes_the_first_free_phone(db, clock):
    take(db, device="dev_a", holder="agent-1")

    got = leases.acquire(db, ["dev_a", "dev_b"], "agent-2", 60, "qa")

    assert got.device_id == "dev_b"


def test_a_full_pool_names_the_holder_that_frees_first(db, clock):
    take(db, device="dev_a", holder="agent-1", ttl=300)
    take(db, device="dev_b", holder="agent-2", ttl=100)

    with pytest.raises(leases.DeviceBusy) as busy:
        leases.acquire(db, ["dev_a", "dev_b"], "agent-3", 60, "qa")

    assert busy.value.holder.holder == "agent-2"


def test_the_restricted_mode_is_stored_and_listed(db, clock):
    take(db, restricted=True)

    [row] = leases.active(db)

    assert row.restricted is True
    assert row.space_epoch is None  # filled when spaces (CHE-1387) bump the device epoch


# A crash is a process that dies with no cleanup. SQLite decides each boundary: the
# state after the crash must be the state before the statement or the state after commit.
_CRASH = """
import os
import sys
from pathlib import Path
from apps.admin_console.services import device_leases as leases

db, boundary, action = Path(sys.argv[1]), *sys.argv[2:4]
leases.boundary = lambda name: os._exit(9) if name == boundary else None
if action == "acquire":
    leases.acquire(db, ["dev_a"], "agent-1", 600, "qa")
elif action == "renew":
    leases.renew(db, sys.argv[4], "agent-1", 900)
else:
    leases.release(db, sys.argv[4], "agent-1")
"""


def crash(db, boundary, action, *args):
    done = subprocess.run(
        [sys.executable, "-c", _CRASH, str(db), boundary, action, *args],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 9, done.stderr


def test_a_crash_before_the_reserve_commits_leaves_the_phone_free(db):
    crash(db, "acquire:reserved", "acquire")

    assert leases.active(db) == []
    assert take(db, holder="agent-2").holder == "agent-2"


def test_a_crash_after_the_reserve_commits_keeps_the_lease_until_it_expires(db, clock):
    crash(db, "acquire:committed", "acquire")
    clock[0] = leases.time.time()

    [row] = leases.active(db)
    assert row.holder == "agent-1"
    with pytest.raises(leases.DeviceBusy):
        take(db, holder="agent-2")

    clock[0] = row.expires_at
    assert take(db, holder="agent-2").holder == "agent-2"


def test_a_crash_before_the_renew_commits_keeps_the_old_expiry(db):
    lease = leases.acquire(db, ["dev_a"], "agent-1", 600, "qa")

    crash(db, "renew:updated", "renew", lease.lease_id)

    assert leases.active(db)[0].expires_at == lease.expires_at


def test_a_crash_after_the_renew_commits_keeps_the_new_expiry(db):
    lease = leases.acquire(db, ["dev_a"], "agent-1", 600, "qa")

    crash(db, "renew:committed", "renew", lease.lease_id)

    assert leases.active(db)[0].expires_at > lease.expires_at + 200


def test_a_crash_before_the_release_commits_keeps_the_lease(db):
    lease = leases.acquire(db, ["dev_a"], "agent-1", 600, "qa")

    crash(db, "release:deleted", "release", lease.lease_id)

    assert [row.lease_id for row in leases.active(db)] == [lease.lease_id]


def test_a_crash_after_the_release_commits_frees_the_phone(db):
    lease = leases.acquire(db, ["dev_a"], "agent-1", 600, "qa")

    crash(db, "release:committed", "release", lease.lease_id)

    assert leases.active(db) == []
