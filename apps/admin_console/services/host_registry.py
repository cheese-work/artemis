"""Computer registry and host-agent credentials (CHE-1095, release B1).

One SQLite-backed store for enrolled computers (hosts), their phones, enrollment
codes and session tokens. Additive tables in the same database file; nothing
here runs unless ``ARTEMIS_HOST_AGENT=enabled``.

Trust model: the agent holds an Ed25519 key that never leaves its computer. An
enrollment code is a one-time, 15-minute bearer that is bound to the first key
that proves possession of it. Every connection is a signed challenge; the
resulting 24-hour token is bound to host id and connection generation.
"""

from __future__ import annotations

import base64
import binascii
from collections import deque
from contextlib import closing, contextmanager
import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from apps.admin_console.database.connection import get_db

# Shared protocol constants (the agent, server, UI and docs read these).
PROTOCOL_VERSION = 1
MIN_SUPPORTED = 1
CODE_TTL_SECONDS = 15 * 60
TOKEN_TTL_SECONDS = 24 * 3600
NONCE_TTL_SECONDS = 60
MAX_CLOCK_SKEW_SECONDS = 60
ENROLL_IP_LIMIT = (20, 60)
CHALLENGE_IP_LIMIT = (30, 60)
CODE_ATTEMPT_LIMIT = 10
MAX_PENDING_NONCES = 2048
MAX_DEVICES_PER_HOST = 64
SHARE_COMMAND = "smartqa-host share <serial>"

_SERIAL = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_enrollment_codes (
    id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL,
    key_hash TEXT, host_id TEXT, used_at REAL
);
CREATE TABLE IF NOT EXISTS hosts (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, os TEXT, agent_version TEXT,
    protocol_version INTEGER, public_key TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL, created_at REAL NOT NULL,
    status TEXT NOT NULL, reason TEXT, since REAL NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0, last_seen REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS host_devices (
    host_id TEXT NOT NULL, serial TEXT NOT NULL, model TEXT, shared INTEGER NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (host_id, serial)
);
CREATE TABLE IF NOT EXISTS host_tokens (
    token_hash TEXT PRIMARY KEY, host_id TEXT NOT NULL, generation INTEGER NOT NULL,
    scopes TEXT NOT NULL, expires_at REAL NOT NULL
);
"""


def host_agent_enabled() -> bool:
    return os.environ.get("ARTEMIS_HOST_AGENT", "").strip().casefold() == "enabled"


def enroll_message(code: str, public_key: str) -> bytes:
    return f"artemis-host-enroll/v1\n{code}\n{public_key}".encode()


def connect_message(
    audience: str, protocol: int, host_id: str, nonce: str, timestamp: int
) -> bytes:
    return (
        f"artemis-host-connect/v1\n{audience}\n{protocol}\n{host_id}\n{nonce}\n{timestamp}".encode()
    )


class RegistryError(Exception):
    """A refusal with a stable code; routers map it to HTTP or a socket close."""

    def __init__(self, code: str, status: int = 400, **extra: Any):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra


class _Limiter:
    """Sliding-window hit counter; process-local (one server process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float, now: float) -> bool:
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # ponytail: drop idle keys when the table grows
                for stale in [k for k, h in self._hits.items() if not h or h[-1] <= now - window]:
                    del self._hits[stale]
            return True

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


def _sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _decode(value: object, size: int, code: str) -> bytes:
    try:
        raw = base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != size:
        raise RegistryError(code, 422)
    return raw


def _verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def _clean(value: object, limit: int, default: str = "") -> str:
    text = " ".join(str(value or "").split())[:limit]
    return text or default


class HostRegistry:
    def __init__(self) -> None:
        self.db_path = None  # None = the shared Artemis database
        self.clock = time.time
        self._schema_ready: set[str] = set()
        self._nonces: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()
        self.limiter = _Limiter()

    # -- plumbing ---------------------------------------------------------

    @contextmanager
    def _db(self):
        with closing(get_db(self.db_path)) as conn:
            key = str(self.db_path)
            if key not in self._schema_ready:
                conn.executescript(_SCHEMA)
                self._schema_ready.add(key)
            with conn:  # commit on success, roll back on any error
                yield conn

    def reset_memory(self) -> None:
        self.limiter.clear()
        with self._lock:
            self._nonces.clear()
            self._schema_ready.clear()

    def allow(self, key: str, limit: int, window: float) -> bool:
        return self.limiter.allow(key, limit, window, self.clock())

    def reset_for_boot(self) -> None:
        """No connection survives a restart: every live row starts offline."""
        now = self.clock()
        with self._db() as conn:
            conn.execute(
                "UPDATE hosts SET status='offline', reason='server_restarted', since=? "
                "WHERE revoked_at IS NULL",
                (now,),
            )

    # -- enrollment codes ---------------------------------------------------

    def create_code(self, created_by: str) -> dict[str, Any]:
        code = secrets.token_urlsafe(16)  # 128 bits
        now = self.clock()
        code_id = secrets.token_hex(8)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO host_enrollment_codes (id, code_hash, created_by, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (code_id, _sha256(code), created_by, now, now + CODE_TTL_SECONDS),
            )
        return {"code_id": code_id, "code": code, "expires_at": now + CODE_TTL_SECONDS}

    def code_is_valid(self, code: str) -> bool:
        with self._db() as conn:
            row = conn.execute(
                "SELECT expires_at FROM host_enrollment_codes WHERE code_hash=?", (_sha256(code),)
            ).fetchone()
        return row is not None and row["expires_at"] > self.clock()

    def code_status(self, code_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT c.expires_at, c.host_id, h.name FROM host_enrollment_codes c "
                "LEFT JOIN hosts h ON h.id = c.host_id WHERE c.id=?",
                (code_id,),
            ).fetchone()
        if row is None:
            return None
        if row["host_id"]:
            return {
                "status": "connected",
                "computer_name": row["name"],
                "computer_id": row["host_id"],
            }
        expired = row["expires_at"] <= self.clock()
        return {"status": "expired" if expired else "waiting", "computer_name": None}

    def enroll(self, body: dict[str, Any]) -> dict[str, Any]:
        code = str(body.get("code", ""))
        public_key = str(body.get("public_key", ""))
        raw_key = _decode(public_key, 32, "key_invalid")
        signature = _decode(body.get("signature"), 64, "signature_invalid")
        code_hash = _sha256(code)
        now = self.clock()
        if not self.allow(f"code:{code_hash}", CODE_ATTEMPT_LIMIT, CODE_TTL_SECONDS):
            raise RegistryError("rate_limited", 429)
        if not _verify(raw_key, signature, enroll_message(code, public_key)):
            raise RegistryError("signature_invalid", 401)
        key_hash = _sha256(raw_key)
        host_id = key_hash[:32]
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM host_enrollment_codes WHERE code_hash=?", (code_hash,)
            ).fetchone()
            if row is None:
                raise RegistryError("code_invalid", 404)
            if row["expires_at"] <= now:
                raise RegistryError("code_expired", 410)
            if row["key_hash"] is not None and row["key_hash"] != key_hash:
                raise RegistryError("code_used", 409)
            existing = conn.execute(
                "SELECT revoked_at FROM hosts WHERE id=?", (host_id,)
            ).fetchone()
            if existing is not None and existing["revoked_at"] is not None:
                raise RegistryError("host_revoked", 409)
            conn.execute(
                "UPDATE host_enrollment_codes SET key_hash=?, host_id=?, used_at=COALESCE(used_at, ?)"
                " WHERE id=?",
                (key_hash, host_id, now, row["id"]),
            )
            conn.execute(
                "INSERT INTO hosts (id, name, os, agent_version, protocol_version, public_key, "
                "key_hash, created_by, created_at, status, reason, since) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'offline', 'never_connected', ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, os=excluded.os, "
                "agent_version=excluded.agent_version, protocol_version=excluded.protocol_version",
                (
                    host_id,
                    _clean(body.get("name"), 64, "computer"),
                    _clean(body.get("os"), 64),
                    _clean(body.get("agent_version"), 32),
                    int(body.get("protocol_version") or 0),
                    public_key,
                    key_hash,
                    row["created_by"],
                    now,
                    now,
                ),
            )
        return {
            "host_id": host_id,
            "protocol_version": PROTOCOL_VERSION,
            "min_supported": MIN_SUPPORTED,
        }

    # -- handshake ----------------------------------------------------------

    def issue_nonce(self, host_id: str, audience: str) -> str | None:
        now = self.clock()
        nonce = secrets.token_urlsafe(24)
        with self._lock:
            if len(self._nonces) >= MAX_PENDING_NONCES:
                self._nonces = {k: v for k, v in self._nonces.items() if v[2] > now}
            if len(self._nonces) >= MAX_PENDING_NONCES:
                return None
            self._nonces[nonce] = (host_id, audience, now + NONCE_TTL_SECONDS)
        return nonce

    def _consume_nonce(self, nonce: object) -> tuple[str, str] | None:
        with self._lock:
            entry = self._nonces.pop(str(nonce), None)
        if entry is None or entry[2] <= self.clock():
            return None
        return entry[0], entry[1]

    def authenticate_hello(self, hello: object, audience: str) -> dict[str, Any]:
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise RegistryError("bad_request", 4400)
        # The nonce is spent on first presentation, valid or not.
        issued = self._consume_nonce(hello.get("nonce"))
        if issued is None:
            raise RegistryError("nonce_invalid", 4401)
        host_id, nonce_audience = issued
        try:
            protocol = int(hello["protocol_version"])
            timestamp = int(hello["timestamp"])
            signature = base64.b64decode(str(hello["signature"]), validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise RegistryError("bad_request", 4400) from None
        with self._db() as conn:
            host = conn.execute("SELECT * FROM hosts WHERE id=?", (host_id,)).fetchone()
        message = connect_message(
            nonce_audience, protocol, str(hello.get("host_id")), str(hello["nonce"]), timestamp
        )
        if (
            host is None
            or nonce_audience != audience
            or hello.get("host_id") != host_id
            or not _verify(base64.b64decode(host["public_key"]), signature, message)
        ):
            raise RegistryError("signature_invalid", 4401)
        if host["revoked_at"] is not None:
            raise RegistryError("host_revoked", 4410)
        if abs(timestamp - self.clock()) > MAX_CLOCK_SKEW_SECONDS:
            raise RegistryError("clock_skew", 4401)
        if protocol < MIN_SUPPORTED:
            self._set_status(host_id, "update_required", "update_required")
            raise RegistryError("update_required", 4426, min_supported=MIN_SUPPORTED)
        return {
            **dict(host),
            "agent_version": _clean(hello.get("agent_version"), 32),
            "protocol_version": protocol,
        }

    def _set_status(self, host_id: str, status: str, reason: str | None) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE hosts SET status=?, reason=?, since=? WHERE id=? AND revoked_at IS NULL",
                (status, reason, self.clock(), host_id),
            )

    def begin_connection(self, host: dict[str, Any]) -> dict[str, Any]:
        now = self.clock()
        token = secrets.token_urlsafe(32)
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE hosts SET generation=generation+1, status='online', reason=NULL, since=?, "
                "last_seen=?, agent_version=?, protocol_version=? WHERE id=? AND revoked_at IS NULL",
                (now, now, host["agent_version"], host["protocol_version"], host["id"]),
            )
            row = conn.execute(
                "SELECT generation, revoked_at FROM hosts WHERE id=?", (host["id"],)
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise RegistryError("host_revoked", 4410)
            conn.execute("DELETE FROM host_tokens WHERE host_id=?", (host["id"],))
            conn.execute(
                "INSERT INTO host_tokens (token_hash, host_id, generation, scopes, expires_at) "
                "VALUES (?, ?, ?, 'connect,uploads', ?)",
                (_sha256(token), host["id"], row["generation"], now + TOKEN_TTL_SECONDS),
            )
        return {
            "generation": row["generation"],
            "token": token,
            "expires_at": now + TOKEN_TTL_SECONDS,
        }

    def end_connection(self, host_id: str, generation: int, reason: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE hosts SET status='offline', reason=?, since=?, last_seen=? "
                "WHERE id=? AND generation=? AND status='online'",
                (reason, self.clock(), self.clock(), host_id, generation),
            )

    # -- tokens ---------------------------------------------------------------

    def validate_token(self, token: str, scope: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT h.*, t.scopes, t.expires_at AS token_expires_at FROM host_tokens t "
                "JOIN hosts h ON h.id = t.host_id AND h.generation = t.generation "
                "WHERE t.token_hash=? AND h.revoked_at IS NULL",
                (_sha256(token),),
            ).fetchone()
        if (
            row is None
            or row["token_expires_at"] <= self.clock()
            or scope not in row["scopes"].split(",")
        ):
            return None
        return dict(row)

    def renew(self, token: str) -> float | None:
        if self.validate_token(token, "connect") is None:
            return None
        expires_at = self.clock() + TOKEN_TTL_SECONDS
        with self._db() as conn:
            conn.execute(
                "UPDATE host_tokens SET expires_at=? WHERE token_hash=?",
                (expires_at, _sha256(token)),
            )
        return expires_at

    # -- admin actions ----------------------------------------------------------

    def revoke(self, host_id: str) -> bool:
        with self._db() as conn:
            found = conn.execute("SELECT 1 FROM hosts WHERE id=?", (host_id,)).fetchone()
            if found is None:
                return False
            conn.execute(
                "UPDATE hosts SET status='revoked', reason='revoked', since=?, "
                "revoked_at=COALESCE(revoked_at, ?) WHERE id=?",
                (self.clock(), self.clock(), host_id),
            )
            conn.execute("DELETE FROM host_tokens WHERE host_id=?", (host_id,))
        return True

    def rename(self, host_id: str, name: str) -> bool:
        with self._db() as conn:
            return (
                conn.execute(
                    "UPDATE hosts SET name=? WHERE id=?", (_clean(name, 64, "computer"), host_id)
                ).rowcount
                > 0
            )

    # -- devices and listing --------------------------------------------------------

    def set_devices(self, host_id: str, devices: object) -> None:
        rows = []
        for item in devices if isinstance(devices, list) else []:
            if isinstance(item, dict) and _SERIAL.match(str(item.get("serial", ""))):
                rows.append(
                    (
                        host_id,
                        item["serial"],
                        _clean(item.get("model"), 64) or None,
                        int(item.get("shared") is True),
                        self.clock(),
                    )
                )
        with self._db() as conn:
            conn.execute("DELETE FROM host_devices WHERE host_id=?", (host_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO host_devices VALUES (?,?,?,?,?)",
                rows[:MAX_DEVICES_PER_HOST],
            )

    def list_hosts(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (computers, phones that computers share)."""
        with self._db() as conn:
            hosts = [dict(r) for r in conn.execute("SELECT * FROM hosts ORDER BY created_at, id")]
            devices = [
                dict(r) for r in conn.execute("SELECT * FROM host_devices ORDER BY host_id, serial")
            ]
        by_host: dict[str, list[dict[str, Any]]] = {}
        for device in devices:
            by_host.setdefault(device["host_id"], []).append(device)
        views, shared_phones = [], []
        for host in hosts:
            mine = by_host.get(host["id"], [])
            online = host["status"] == "online"
            views.append(
                {
                    "id": host["id"],
                    "name": host["name"],
                    "os": host["os"],
                    "agent_version": host["agent_version"],
                    "protocol_version": host["protocol_version"],
                    "status": host["status"],
                    "reason": host["reason"],
                    "since": host["since"],
                    "generation": host["generation"],
                    "last_seen": host["last_seen"],
                    "created_at": host["created_at"],
                    "phones_shared": sum(1 for d in mine if d["shared"]),
                    "phones_not_shared": sum(1 for d in mine if not d["shared"]),
                    "unshared_serials": [d["serial"] for d in mine if not d["shared"]],
                    "share_command": SHARE_COMMAND,
                }
            )
            shared_phones.extend(
                {
                    "serial": d["serial"],
                    "model": d["model"],
                    "source": "computer",
                    "computer_id": host["id"],
                    "computer_name": host["name"],
                    "computer_status": host["status"],
                    "reason": None if online else host["reason"],
                    "since": host["since"],
                }
                for d in mine
                if d["shared"]
            )
        return views, shared_phones


host_registry = HostRegistry()
