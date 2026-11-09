"""DLP-1 broker domain. SQLite only; no device, network or Multica I/O.

The owner supplies a trusted admission snapshot (canonical id -> endpoint).
Inventory alone never admits a device. This is separate from the legacy
CHE-2099 authority until its explicitly approved migration/cutover.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from registry import Device, DeviceRegistry

from device_lease import canonical_ids, write_transaction


_DDL = (
    "CREATE TABLE IF NOT EXISTS broker_groups ("
    "lease_id TEXT PRIMARY KEY, principal TEXT NOT NULL, run_id TEXT NOT NULL, "
    "purpose TEXT NOT NULL, started_at REAL NOT NULL, expires_at REAL NOT NULL, "
    "ttl INTEGER NOT NULL, hard_deadline REAL NOT NULL, state TEXT NOT NULL, "
    "recovery_owner TEXT, ops_issue TEXT)",
    "CREATE TABLE IF NOT EXISTS broker_devices ("
    "lease_id TEXT NOT NULL, device_id TEXT NOT NULL, endpoint TEXT NOT NULL, "
    "restricted INTEGER NOT NULL, space_epoch INTEGER, PRIMARY KEY(lease_id, device_id))",
    "CREATE TABLE IF NOT EXISTS broker_runs ("
    "principal TEXT NOT NULL, run_id TEXT NOT NULL, started_at REAL NOT NULL, "
    "hard_deadline REAL NOT NULL, PRIMARY KEY(principal, run_id))",
    "CREATE TABLE IF NOT EXISTS broker_requests ("
    "principal TEXT NOT NULL, run_id TEXT NOT NULL, key TEXT NOT NULL, "
    "fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(principal, run_id, key))",
)


class Refused(Exception):
    def __init__(self, code, **details):
        self.code, self.details = code, details
        super().__init__(code)


class Broker:
    def __init__(
        self,
        db_path,
        registry,
        *,
        admitted=None,
        clock=time.time,
        boundary=lambda name: None,
        conflicting_waiter=lambda conn, ids: False,
    ):
        self.db_path = Path(db_path)
        self.registry = registry
        self.admitted = dict(admitted or {})
        self.clock, self.boundary = clock, boundary
        # Pure SQL hook for slice 3's eligible, conflicting queue entries.
        self.conflicting_waiter = conflicting_waiter
        self.draining = False

    def _owned(self, conn, lease_id, principal, run_id):
        group = self._view(conn, lease_id)
        if (group["holder"], group["run_id"]) != (principal, run_id):
            raise Refused("lease_not_yours")
        return group

    def status(self, *, lease_id, principal, run_id):
        self._text(lease_id, principal, run_id)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            return self._owned(conn, lease_id, principal, run_id)

    def renew(self, *, lease_id, principal, run_id, ttl=1800, idempotency_key):
        self._text(lease_id, principal, run_id, idempotency_key)
        self._ttl(ttl)
        fingerprint = json.dumps(["renew", lease_id, ttl])
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            replay = self._replay(conn, principal, run_id, idempotency_key, fingerprint)
            if replay is not None:
                return replay
            if now >= group["expires_at"]:
                raise Refused("lease_expired")
            if group["state"] != "active":
                raise Refused("lease_not_active")
            if self.draining:
                raise Refused("draining")
            if group["expires_at"] - now > group["ttl"] / 3:
                raise Refused("renew_too_early")
            ids = [d["device_id"] for d in group["devices"]]
            if any(self.registry.resolve(d) is None for d in ids):
                raise Refused("device_unknown")
            maximum = 1800 if any(self.registry.resolve(d).pool == "root" for d in ids) else 3600
            self._ttl(ttl, maximum)
            if self.conflicting_waiter(conn, frozenset(ids)):
                raise Refused("conflicting_waiter")
            expiry = min(now + ttl, group["hard_deadline"])
            conn.execute(
                "UPDATE broker_groups SET expires_at=?, ttl=? WHERE lease_id=?",
                (expiry, ttl, lease_id),
            )
            self.boundary("renew:updated")
            response = self._view(conn, lease_id)
            self._record(conn, principal, run_id, idempotency_key, fingerprint, response)
        self.boundary("renew:committed")
        return response

    def release(self, *, lease_id, principal, run_id):
        self._text(lease_id, principal, run_id)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            if group["state"] == "active":
                conn.execute(
                    "UPDATE broker_groups SET state='fencing' WHERE lease_id=?", (lease_id,)
                )
            return self._view(conn, lease_id)

    def recovery(self, lease_id, expected, target, *, recovery_owner=None, ops_issue=None):
        """Trusted recovery-worker CAS, never a holder RPC. No automatic free path.

        Actual fencing/cleanup and leader-verified release belong to slice 4.
        Until then, even 'verifying' is unavailable and cannot be client-freed.
        """
        allowed = {("fencing", "cleaning"), ("cleaning", "verifying")}
        if target == "quarantine" and expected in {"fencing", "cleaning", "verifying"}:
            if not recovery_owner or not ops_issue:
                raise Refused("recovery_owner_required")
            self._text(recovery_owner, ops_issue)
        elif (expected, target) not in allowed:
            raise Refused("invalid_transition")
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            group = self._view(conn, lease_id)
            if group["state"] != expected:
                raise Refused("state_conflict")
            conn.execute(
                "UPDATE broker_groups SET state=?, recovery_owner=?, ops_issue=? WHERE lease_id=?",
                (target, recovery_owner, ops_issue, lease_id),
            )
            return self._view(conn, lease_id)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            for statement in _DDL:
                conn.execute(statement)
            with write_transaction(conn, self.clock, refusals=(Refused,)) as value:
                yield value
        finally:
            conn.close()

    @staticmethod
    def _text(*values):
        if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
            raise Refused("invalid_request")

    @staticmethod
    def _ttl(ttl, maximum=3600):
        if type(ttl) is not int or not 300 <= ttl <= maximum:
            raise Refused("invalid_request")

    def _canonical(self, alias):
        device = self.registry.resolve(alias)
        return device.device_id if device else None

    def _expire(self, conn, now):
        # Expiry never frees ownership. Recovery must prove release first.
        conn.execute(
            "UPDATE broker_groups SET state='fencing' WHERE state='active' AND expires_at <= ?",
            (now,),
        )

    @staticmethod
    def _view(conn, lease_id):
        row = conn.execute("SELECT * FROM broker_groups WHERE lease_id=?", (lease_id,)).fetchone()
        if row is None:
            raise Refused("lease_unknown")
        group = dict(row)
        group["holder"] = group.pop("principal")
        devices = conn.execute(
            "SELECT device_id, endpoint, restricted, space_epoch "
            "FROM broker_devices WHERE lease_id=? ORDER BY device_id",
            (lease_id,),
        ).fetchall()
        group["devices"] = [dict(d, restricted=bool(d["restricted"])) for d in devices]
        return group

    @staticmethod
    def _replay(conn, principal, run_id, key, fingerprint):
        row = conn.execute(
            "SELECT fingerprint, response FROM broker_requests "
            "WHERE principal=? AND run_id=? AND key=?",
            (principal, run_id, key),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise Refused("idempotency_conflict")
        return json.loads(row["response"])

    @staticmethod
    def _record(conn, principal, run_id, key, fingerprint, response):
        conn.execute(
            "INSERT INTO broker_requests VALUES (?,?,?,?,?)",
            (principal, run_id, key, fingerprint, json.dumps(response)),
        )

    def acquire(
        self,
        *,
        principal,
        run_id,
        purpose,
        pool="rig",
        device_id=None,
        count=1,
        ttl=1800,
        idempotency_key,
    ):
        self._text(principal, run_id, purpose, pool, idempotency_key)
        self._ttl(ttl, 1800 if pool == "root" else 3600)
        if type(count) is not int or count < 1:
            raise Refused("invalid_request")
        if device_id is not None:
            self._text(device_id)
        with self.transaction() as (conn, now):
            target = self._canonical(device_id) if device_id else None
            fingerprint = json.dumps(["acquire", pool, target or device_id, count, ttl, purpose])
            self._expire(conn, now)
            replay = self._replay(conn, principal, run_id, idempotency_key, fingerprint)
            if replay is not None:
                return replay
            if self.draining:
                raise Refused("draining")
            ids = canonical_ids([device_id] if device_id else self.admitted, self._canonical)
            candidates = []
            for canonical in ids:
                device = self.registry.resolve(canonical)
                endpoint = self.admitted.get(canonical)
                if (
                    device.pool == pool
                    and endpoint
                    and self._canonical(endpoint) == canonical
                    and self.registry.eligible(endpoint, "device")
                ):
                    candidates.append(canonical)
            if count > len(candidates):
                raise Refused("impossible_batch")
            held = conn.execute(
                "SELECT lease_id FROM broker_groups WHERE principal=? "
                "AND run_id=? AND state != 'released'",
                (principal, run_id),
            ).fetchone()
            if held:
                raise Refused("run_already_holds")
            occupied = {
                r["device_id"]: r["lease_id"]
                for r in conn.execute(
                    "SELECT d.device_id, d.lease_id FROM broker_devices d JOIN broker_groups g "
                    "ON d.lease_id=g.lease_id WHERE g.state != 'released'"
                )
            }
            free = [d for d in candidates if d not in occupied]
            if len(free) < count:
                holders = [self._view(conn, occupied[d]) for d in candidates if d in occupied]
                raise Refused("device_busy", holder=min(holders, key=lambda g: g["expires_at"]))
            budget = conn.execute(
                "SELECT * FROM broker_runs WHERE principal=? AND run_id=?", (principal, run_id)
            ).fetchone()
            started = budget["started_at"] if budget else now
            cap = 3600 if pool == "root" else 7200
            deadline = min(started + cap, budget["hard_deadline"]) if budget else now + cap
            if now >= deadline:
                raise Refused("hard_cap")
            lease_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO broker_groups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    principal,
                    run_id,
                    purpose,
                    started,
                    min(now + ttl, deadline),
                    ttl,
                    deadline,
                    "active",
                    None,
                    None,
                ),
            )
            for device_id in free[:count]:
                device = self.registry.resolve(device_id)
                conn.execute(
                    "INSERT INTO broker_devices VALUES (?,?,?,?,?)",
                    (lease_id, device_id, self.admitted[device_id], int(device.restricted), None),
                )
                self.boundary("acquire:device")
            conn.execute(
                "INSERT INTO broker_runs VALUES (?,?,?,?) ON CONFLICT(principal,run_id) "
                "DO UPDATE SET hard_deadline=excluded.hard_deadline",
                (principal, run_id, started, deadline),
            )
            response = self._view(conn, lease_id)
            self._record(conn, principal, run_id, idempotency_key, fingerprint, response)
        self.boundary("acquire:committed")
        return response
