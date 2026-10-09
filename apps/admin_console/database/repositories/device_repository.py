"""Durable devices: records, connections and aliases (CHE-1472; contract: docs/device-identity.md).

Ids are opaque and never derived from a serial. A ``{source, host_id, serial}``
connection key belongs to exactly one device; moving it is Merge or Split.
"""

from dataclasses import dataclass
import sqlite3
import time
import uuid

from apps.admin_console.database.connection import db_session


class DeviceStoreNotReady(Exception):
    """The device tables are missing (schema bootstrap did not run or failed)."""


class ConnectionKeyTaken(Exception):
    """The connection key already belongs to another device."""


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    owner_principal_id: str | None
    label: str | None
    hardware_hash: str | None
    match_state: str


_COLUMNS = "device_id, owner_principal_id, label, hardware_hash, match_state"
_TABLES = ("devices", "device_connections", "device_aliases")


class DeviceRepository:
    def __init__(self, db_path=None):
        self.db_path = db_path

    def create_device(
        self,
        *,
        owner_principal_id: str | None,
        label: str | None,
        hardware_hash: str | None = None,
        match_state: str = "provisional",
    ) -> Device:
        device = Device(
            f"dev_{uuid.uuid4().hex}", owner_principal_id, label, hardware_hash, match_state
        )
        now = time.time()
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            with conn:
                conn.execute(
                    f"INSERT INTO devices ({_COLUMNS}, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*(getattr(device, k) for k in Device.__slots__), now, now),
                )
        return device

    def get(self, device_id: str) -> Device | None:
        """The canonical device for ``device_id`` or any of its aliases."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            canonical = self._resolve(conn, device_id)
            if canonical is None:
                return None
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM devices WHERE device_id = ?", (canonical,)
            ).fetchone()
            return Device(*row)

    def list(self, *, owner_principal_id: str | None = None) -> list[Device]:
        query, args = f"SELECT {_COLUMNS} FROM devices", ()
        if owner_principal_id is not None:
            query, args = f"{query} WHERE owner_principal_id = ?", (owner_principal_id,)
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return [Device(*row) for row in conn.execute(f"{query} ORDER BY rowid", args)]

    def resolve(self, device_id: str) -> str | None:
        """The canonical ``device_id``, or None when the id is unknown."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            return self._resolve(conn, device_id)

    def add_alias(self, alias_device_id: str, device_id: str) -> None:
        """Keep ``alias_device_id`` resolvable to ``device_id``; existing chains collapse to one hop."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                target = self._resolve(conn, device_id)
                if target is None or target == alias_device_id:
                    raise ValueError(f"cannot alias {alias_device_id!r} to {device_id!r}")
                conn.execute(
                    "UPDATE device_aliases SET device_id = ? WHERE device_id = ?",
                    (target, alias_device_id),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO device_aliases (alias_device_id, device_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (alias_device_id, target, time.time()),
                )

    def upsert_connection(
        self, device_id: str, *, kind: str, source: str, host_id: str | None, serial: str
    ) -> str:
        """The ``connection_id`` for this key on ``device_id``; created on first sight.

        Raises ConnectionKeyTaken when the key already belongs to another device.
        """
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                canonical = self._resolve(conn, device_id)
                if canonical is None:
                    raise ValueError(f"unknown device {device_id!r}")
                now = time.time()
                row = self._find_connection(conn, source, host_id, serial)
                if row is None:
                    connection_id = f"con_{uuid.uuid4().hex}"
                    conn.execute(
                        "INSERT INTO device_connections (connection_id, device_id, kind, source, "
                        "host_id, serial, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (connection_id, canonical, kind, source, host_id, serial, now, now),
                    )
                    return connection_id
                if row["device_id"] != canonical:
                    raise ConnectionKeyTaken(row["connection_id"])
                conn.execute(
                    "UPDATE device_connections SET kind = ?, last_seen_at = ? WHERE connection_id = ?",
                    (kind, now, row["connection_id"]),
                )
                return row["connection_id"]

    def device_for_connection(self, source: str, host_id: str | None, serial: str) -> str | None:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            row = self._find_connection(conn, source, host_id, serial)
            return row["device_id"] if row else None

    @staticmethod
    def _find_connection(conn: sqlite3.Connection, source: str, host_id: str | None, serial: str):
        return conn.execute(
            "SELECT connection_id, device_id FROM device_connections "
            "WHERE source = ? AND ifnull(host_id, '') = ifnull(?, '') AND serial = ?",
            (source, host_id, serial),
        ).fetchone()

    @staticmethod
    def _resolve(conn: sqlite3.Connection, device_id: str) -> str | None:
        row = conn.execute(
            "SELECT device_id FROM device_aliases WHERE alias_device_id = ? "
            "UNION ALL SELECT device_id FROM devices WHERE device_id = ? LIMIT 1",
            (device_id, device_id),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _require_ready(conn: sqlite3.Connection) -> None:
        (found,) = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            f"AND name IN ({', '.join('?' * len(_TABLES))})",
            _TABLES,
        ).fetchone()
        if found != len(_TABLES):
            raise DeviceStoreNotReady


device_repo = DeviceRepository()
