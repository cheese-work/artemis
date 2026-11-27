"""Durable devices: records, connections and aliases (CHE-1472; contract: docs/device-identity.md).

Ids are opaque and never derived from a serial. A ``{source, host_id, serial}``
connection key belongs to exactly one device; moving it is Merge or Split.
"""

from dataclasses import dataclass
import json
import sqlite3
import time
import uuid

from apps.admin_console.core.access_control import AdminAPIError
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
    """Where one connection landed: ``outcome`` is confirmed, uncertain or provisional.

    ``outcome`` is what this observation proves. A non-confirmed outcome on a known
    route names the device that route last held; it is not resolved to it.
    """

    device_id: str
    connection_id: str
    outcome: str


_COLUMNS = "device_id, owner_principal_id, label, hardware_hash, match_state"
# Sources whose routing key is a transport, not an identity. A host agent's opaque id is
# derived from the phone's hardware id (or AVD name), so a host route identifies its phone.
REUSABLE_SOURCES = frozenset({"local", "bridge"})
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
            if hardware_hash is None and source in REUSABLE_SOURCES:
                # A server adb serial, ip:port or bridge port can carry another phone: only
                # a fresh hardware identity proves this one, so the device stays as it is.
                return Match(device_id, connection_id, "uncertain" if readable else "provisional")
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
        if self._kept_separate(conn, device_id, holder["device_id"]):
            return device_id, "confirmed"  # the owner decided: this pair is never offered again
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

    def merge(
        self, device_id: str, other_device_id: str, *, actor_principal_id: str, now: float
    ) -> Device:
        """``other_device_id`` joins the survivor ``device_id``, which ends ``confirmed``.

        Callers serialize against queue admission first (``device_merge.merge``).
        Raises ValueError for an unknown or identical pair, or two different hardware
        identities (two phones); AdminAPIError for owners (docs/device-identity.md).
        """
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                survivor, other = self._owned_pair(
                    conn, device_id, other_device_id, actor_principal_id
                )
                if survivor["hardware_hash"] and other["hardware_hash"]:
                    raise ValueError("devices with two hardware identities are two phones")
                self._join(conn, other["device_id"], survivor["device_id"], now)
                conn.execute(  # the hash is freed by the join, so the survivor can take it
                    "UPDATE devices SET match_state = 'confirmed', updated_at = ?, "
                    "hardware_hash = coalesce(hardware_hash, ?) WHERE device_id = ?",
                    (now, other["hardware_hash"], survivor["device_id"]),
                )
                self._event(
                    conn,
                    "merged",
                    survivor["device_id"],
                    other["device_id"],
                    survivor["device_id"],
                    actor_principal_id,
                    now,
                )
        return self.get(survivor["device_id"])

    def keep_separate(
        self, device_id: str, other_device_id: str, *, actor_principal_id: str, now: float
    ) -> None:
        """Record the pair as two phones: both end ``confirmed`` and matching never offers it again."""
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                first, second = self._owned_pair(
                    conn, device_id, other_device_id, actor_principal_id
                )
                low, high = sorted((first["device_id"], second["device_id"]))
                conn.execute(
                    "INSERT OR IGNORE INTO device_non_matches (device_id_a, device_id_b, created_at) "
                    "VALUES (?, ?, ?)",
                    (low, high, now),
                )
                conn.execute(
                    "UPDATE devices SET match_state = 'confirmed', updated_at = ? "
                    "WHERE device_id IN (?, ?)",
                    (now, low, high),
                )
                self._event(
                    conn,
                    "kept_separate",
                    first["device_id"],
                    second["device_id"],
                    None,
                    actor_principal_id,
                    now,
                )

    def _owned_pair(
        self, conn: sqlite3.Connection, device_id: str, other_device_id: str, actor: str
    ):
        """Both records, resolved through aliases, once the actor may decide for them."""
        rows = []
        for requested in (device_id, other_device_id):
            canonical = self._resolve(conn, requested)
            row = (
                canonical
                and conn.execute(
                    f"SELECT {_COLUMNS} FROM devices WHERE device_id = ?", (canonical,)
                ).fetchone()
            )
            if not row:
                raise ValueError(f"unknown device {requested!r}")
            rows.append(row)
        if rows[0]["device_id"] == rows[1]["device_id"]:
            raise ValueError("a device cannot be merged or kept separate from itself")
        owners = {row["owner_principal_id"] for row in rows}
        if owners != {actor}:
            if len(owners) == 2 and actor in owners:
                raise AdminAPIError(
                    409,
                    "The two records have different owners.",
                    "device_identity_unresolved",
                    "Ask an administrator to reassign ownership first.",
                )
            raise AdminAPIError(
                403,
                "This phone belongs to someone else.",
                "device_not_yours",
                "Only the phone's owner can decide.",
            )
        return rows

    @staticmethod
    def _event(conn, kind, device_id, other_device_id, survivor, actor, now) -> None:
        payload = {
            "device_id": device_id,
            "other_device_id": other_device_id,
            "survivor": survivor,
            "actor_principal_id": actor,
        }
        conn.execute(
            "INSERT INTO device_events (device_id, event_key, kind, payload, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                device_id,
                f"{device_id}:{kind}:{other_device_id}:{now!r}",
                kind,
                json.dumps(payload),
                now,
            ),
        )

    @staticmethod
    def _kept_separate(conn: sqlite3.Connection, device_id: str, other_device_id: str) -> bool:
        low, high = sorted((device_id, other_device_id))
        return (
            conn.execute(
                "SELECT 1 FROM device_non_matches WHERE device_id_a = ? AND device_id_b = ?",
                (low, high),
            ).fetchone()
            is not None
        )

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
