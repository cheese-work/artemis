"""Device leases: exclusive, expiring use of a phone (CHE-2099, S3.2).

The single admission authority for "who may use this phone now". A lease is one row,
keyed on the stable ``device_id`` (a unique index, so a device holds at most one).
Every change runs in one ``BEGIN IMMEDIATE`` transaction, so racing callers are
serialized by SQLite and a crash leaves either the state before or the state after
the commit. Nothing runs in the background: an expired row is dead the moment its
time passes and is deleted by the next call that touches the table.

``space_epoch`` is a nullable column, ``NULL`` until spaces (CHE-1387) exist. A device
move there is a compare-and-set inside ``BEGIN IMMEDIATE`` on the same database,
so it serializes against this table without a second lock.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import sqlite3
import time
import uuid

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.database.connection import db_session

clock = time.time


def boundary(name: str) -> None:
    """Crash-test seam: a test replaces this to kill the process at a named commit boundary."""


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    device_id: str
    holder: str
    purpose: str
    restricted: bool
    space_epoch: int | None
    created_at: float
    expires_at: float


class DeviceBusy(Exception):
    def __init__(self, holder: Lease):
        super().__init__(holder.device_id)
        self.holder = holder


class LeaseError(AdminAPIError):
    """A refused renew or release; ``code`` says why."""


_DDL = (
    "CREATE TABLE IF NOT EXISTS device_leases ("
    "lease_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, holder TEXT NOT NULL, "
    "purpose TEXT NOT NULL, restricted INTEGER NOT NULL, space_epoch INTEGER, "
    "created_at REAL NOT NULL, expires_at REAL NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_leases_device ON device_leases (device_id)",
)
_COLUMNS = "lease_id, device_id, holder, purpose, restricted, space_epoch, created_at, expires_at"


def _lease(row: sqlite3.Row | tuple) -> Lease:
    return Lease(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6], row[7])


@contextmanager
def _transaction(db_path):
    """A write transaction: committed on success or refusal, rolled back on any other error."""
    with db_session(db_path) as conn:
        conn.isolation_level = None  # explicit BEGIN/COMMIT below, no implicit ones
        for statement in _DDL:
            conn.execute(statement)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except (DeviceBusy, LeaseError):
            conn.execute("COMMIT")  # a refusal still keeps the purge of expired rows
            raise
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def _purge(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM device_leases WHERE expires_at <= ?", (now,))


def acquire(
    db_path,
    device_ids: list[str],
    holder: str,
    ttl: float,
    purpose: str,
    *,
    restricted: bool = False,
    always_restricted: frozenset[str] = frozenset(),
) -> Lease:
    """Lease the first free phone of ``device_ids``; ``DeviceBusy`` names who frees first.

    A phone in ``always_restricted`` is leased restricted whatever the caller asked.
    """
    now = clock()
    with _transaction(db_path) as conn:
        _purge(conn, now)
        held = []
        for device_id in device_ids:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM device_leases WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is not None:
                held.append(_lease(row))
                continue
            lease = Lease(
                uuid.uuid4().hex,
                device_id,
                holder,
                purpose,
                restricted or device_id in always_restricted,
                None,
                now,
                now + ttl,
            )
            conn.execute(
                f"INSERT INTO device_leases ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lease.lease_id,
                    device_id,
                    holder,
                    purpose,
                    int(lease.restricted),
                    None,
                    now,
                    now + ttl,
                ),
            )
            boundary("acquire:reserved")
            break
        else:
            raise DeviceBusy(min(held, key=lambda lease: lease.expires_at))
    boundary("acquire:committed")
    return lease


def active(db_path) -> list[Lease]:
    with _transaction(db_path) as conn:
        _purge(conn, clock())
        rows = conn.execute(f"SELECT {_COLUMNS} FROM device_leases ORDER BY created_at").fetchall()
    return [_lease(row) for row in rows]


def _owned(conn: sqlite3.Connection, lease_id: str, holder: str, admin: bool, now: float) -> Lease:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM device_leases WHERE lease_id = ?", (lease_id,)
    ).fetchone()
    if row is None:
        raise LeaseError(404, "No such lease.", "lease_unknown", "Acquire a new lease.")
    lease = _lease(row)
    if lease.expires_at <= now:
        _purge(conn, now)
        raise LeaseError(410, "The lease has expired.", "lease_expired", "Acquire a new lease.")
    if holder != lease.holder and not admin:
        raise LeaseError(
            403, "This lease belongs to someone else.", "lease_not_yours", "Use your own lease."
        )
    return lease


def renew(db_path, lease_id: str, holder: str, ttl: float, *, admin: bool = False) -> Lease:
    now = clock()
    with _transaction(db_path) as conn:
        lease = _owned(conn, lease_id, holder, admin, now)
        conn.execute(
            "UPDATE device_leases SET expires_at = ? WHERE lease_id = ?", (now + ttl, lease_id)
        )
        boundary("renew:updated")
    boundary("renew:committed")
    return replace(lease, expires_at=now + ttl)


def release(db_path, lease_id: str, holder: str, *, admin: bool = False) -> None:
    with _transaction(db_path) as conn:
        _owned(conn, lease_id, holder, admin, clock())
        conn.execute("DELETE FROM device_leases WHERE lease_id = ?", (lease_id,))
        boundary("release:deleted")
    boundary("release:committed")
