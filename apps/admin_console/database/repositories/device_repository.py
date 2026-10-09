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


@dataclass(frozen=True, slots=True)
class Match:
    """Where one connection landed: ``outcome`` is confirmed, uncertain or provisional."""

    device_id: str
    connection_id: str
    outcome: str


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
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            with conn:
                self._insert(conn, device, time.time())
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

    def connection(
        self,
        *,
        connection_id: str | None = None,
        source: str | None = None,
        host_id: str | None = None,
        serial: str | None = None,
    ) -> Match | None:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            row = (
                conn.execute(
                    "SELECT connection_id, device_id FROM device_connections WHERE connection_id = ?",
                    (connection_id,),
                ).fetchone()
                if connection_id
                else self._find_connection(conn, source, host_id, serial)
            )
            if row is None:
                return None
            canonical = self._resolve(conn, row["device_id"])
            outcome = conn.execute(
                "SELECT match_state FROM devices WHERE device_id = ?", (canonical,)
            ).fetchone()[0]
            return Match(canonical, row["connection_id"], outcome)

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

    def match_connection(
        self,
        *,
        kind: str,
        source: str,
        host_id: str | None,
        serial: str,
        hardware_hash: str | None,
        readable: bool,
        label: str | None = None,
        previous_serials: tuple[str, ...] = (),
    ) -> Match:
        """Match one connection to a device (docs/device-identity.md, "Matching rules").

        ``hardware_hash`` is None when the connection has no hardware identity;
        ``readable`` is False when adb could not read one yet (provisional).
        ``previous_serials`` are routing keys this phone's connections used before it
        had a hardware identity: one is re-keyed when ``serial`` is new, and every other
        record they left behind joins the confirmed device, never duplicated.
        """
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                now = time.time()
                match = self._match(
                    conn,
                    kind,
                    source,
                    host_id,
                    serial,
                    hardware_hash,
                    readable,
                    label,
                    previous_serials,
                    now,
                )
                if match.outcome == "confirmed":
                    self._absorb(conn, source, host_id, previous_serials, match.device_id, now)
                return match

    def _match(
        self,
        conn: sqlite3.Connection,
        kind: str,
        source: str,
        host_id: str | None,
        serial: str,
        hardware_hash: str | None,
        readable: bool,
        label: str | None,
        previous_serials: tuple[str, ...],
        now: float,
    ) -> Match:
        row = self._find_connection(conn, source, host_id, serial)
        if row is None and hardware_hash is not None:
            row = self._rekey(conn, source, host_id, serial, previous_serials)
        if row is not None:
            device_id, connection_id = row["device_id"], row["connection_id"]
            current = conn.execute(
                "SELECT hardware_hash, match_state, owner_principal_id FROM devices "
                "WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if hardware_hash is None or current["hardware_hash"] in (None, hardware_hash):
                conn.execute(
                    "UPDATE device_connections SET kind = ?, last_seen_at = ? "
                    "WHERE connection_id = ?",
                    (kind, now, connection_id),
                )
                if hardware_hash is None or current["hardware_hash"] == hardware_hash:
                    return Match(device_id, connection_id, current["match_state"])
                device_id, outcome = self._identify(conn, device_id, current, hardware_hash, now)
                return Match(device_id, connection_id, outcome)
            # Another phone took over a recycled transport key (a bridge port, a Wi-Fi
            # address). Retire the old key; its connection keeps its device and history.
            conn.execute(
                "UPDATE device_connections SET serial = serial || '~' || connection_id "
                "WHERE connection_id = ?",
                (connection_id,),
            )
        if hardware_hash is not None:
            holder = self._holder(conn, hardware_hash)
            device_id = holder["device_id"] if holder else None
            outcome = "confirmed"
        else:
            device_id, outcome = None, "uncertain" if readable else "provisional"
        if device_id is None:
            device_id = f"dev_{uuid.uuid4().hex}"
            self._insert(conn, Device(device_id, None, label, hardware_hash, outcome), now)
        connection_id = f"con_{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO device_connections (connection_id, device_id, kind, source, "
            "host_id, serial, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (connection_id, device_id, kind, source, host_id, serial, now, now),
        )
        return Match(device_id, connection_id, outcome)

    def _rekey(
        self,
        conn: sqlite3.Connection,
        source: str,
        host_id: str | None,
        serial: str,
        previous_serials: tuple[str, ...],
    ):
        """The connection a routing-key change left behind, now under ``serial``."""
        for previous in previous_serials:
            row = self._find_connection(conn, source, host_id, previous)
            if row is None:
                continue
            (hashed,) = conn.execute(
                "SELECT hardware_hash FROM devices WHERE device_id = ?", (row["device_id"],)
            ).fetchone()
            if hashed is None:  # a hardware identity is never re-keyed away from its device
                conn.execute(
                    "UPDATE device_connections SET serial = ? WHERE connection_id = ?",
                    (serial, row["connection_id"]),
                )
                return self._find_connection(conn, source, host_id, serial)
        return None

    def _identify(
        self, conn: sqlite3.Connection, device_id: str, current, hardware_hash: str, now: float
    ) -> tuple[str, str]:
        """A record without a hardware identity learns one: ``(device_id, outcome)``."""
        holder = self._holder(conn, hardware_hash)
        if holder is None:
            conn.execute(
                "UPDATE devices SET hardware_hash = ?, match_state = 'confirmed', updated_at = ? "
                "WHERE device_id = ?",
                (hardware_hash, now, device_id),
            )
            return device_id, "confirmed"
        if (
            current["match_state"] != "confirmed"
            and holder["owner_principal_id"] == current["owner_principal_id"]
        ):
            # Never schedulable, so nothing ran on it: the record joins the phone it turned
            # out to be, and its id stays resolvable. Owner choices (Merge) stay in CHE-1480.
            self._join(conn, device_id, holder["device_id"], now)
            return holder["device_id"], "confirmed"
        conn.execute(
            "UPDATE devices SET match_state = 'uncertain', updated_at = ? WHERE device_id = ?",
            (now, device_id),
        )
        return device_id, "uncertain"

    def _absorb(
        self,
        conn: sqlite3.Connection,
        source: str,
        host_id: str | None,
        previous_serials: tuple[str, ...],
        target: str,
        now: float,
    ) -> None:
        """Every other record this host's previous routing keys left behind joins ``target``.

        A hardware identity, an owner's choice (confirmed) and another owner's record
        are never absorbed: those stay for Merge (CHE-1480).
        """
        (owner,) = conn.execute(
            "SELECT owner_principal_id FROM devices WHERE device_id = ?", (target,)
        ).fetchone()
        for previous in previous_serials:
            row = self._find_connection(conn, source, host_id, previous)
            if row is None or row["device_id"] == target:
                continue
            record = conn.execute(
                "SELECT hardware_hash, match_state, owner_principal_id FROM devices "
                "WHERE device_id = ?",
                (row["device_id"],),
            ).fetchone()
            if (
                record["hardware_hash"] is None
                and record["match_state"] != "confirmed"
                and record["owner_principal_id"] == owner
            ):
                self._join(conn, row["device_id"], target, now)

    @staticmethod
    def _join(conn: sqlite3.Connection, device_id: str, target: str, now: float) -> None:
        """``device_id`` joins ``target``: its connections move, its id becomes an alias."""
        conn.execute(
            "UPDATE device_connections SET device_id = ? WHERE device_id = ?", (target, device_id)
        )
        conn.execute(
            "UPDATE device_aliases SET device_id = ? WHERE device_id = ?", (target, device_id)
        )
        conn.execute(
            "INSERT INTO device_aliases (alias_device_id, device_id, created_at) VALUES (?, ?, ?)",
            (device_id, target, now),
        )
        conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))

    def device_for_connection(self, source: str, host_id: str | None, serial: str) -> str | None:
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            row = self._find_connection(conn, source, host_id, serial)
            return row["device_id"] if row else None

    @staticmethod
    def _insert(conn: sqlite3.Connection, device: Device, now: float) -> None:
        conn.execute(
            f"INSERT INTO devices ({_COLUMNS}, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*(getattr(device, k) for k in Device.__slots__), now, now),
        )

    @staticmethod
    def _holder(conn: sqlite3.Connection, hardware_hash: str):
        return conn.execute(
            "SELECT device_id, owner_principal_id FROM devices WHERE hardware_hash = ?",
            (hardware_hash,),
        ).fetchone()

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
