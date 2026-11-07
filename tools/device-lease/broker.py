"""DLP-1 broker domain. SQLite only; no device, network or Multica I/O.

The owner supplies a trusted admission snapshot (canonical id -> endpoint).
Inventory alone never admits a device. This is separate from the legacy
CHE-2099 authority until its explicitly approved migration/cutover.
"""

from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import threading
import uuid

import audit

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
    "CREATE TABLE IF NOT EXISTS broker_decisions ("
    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, entry TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_tickets ("
    "ticket_id TEXT PRIMARY KEY, principal TEXT NOT NULL, agent_id TEXT NOT NULL, "
    "issue_id TEXT NOT NULL, pool TEXT NOT NULL, device_id TEXT, count INTEGER NOT NULL, "
    "ttl INTEGER NOT NULL, position INTEGER NOT NULL, state TEXT NOT NULL, "
    "reserved TEXT NOT NULL DEFAULT '[]', offer_sequence INTEGER NOT NULL DEFAULT 0, "
    "offer_expires_at REAL, missed_offers INTEGER NOT NULL DEFAULT 0)",
    "CREATE UNIQUE INDEX IF NOT EXISTS broker_open_issue ON broker_tickets(issue_id) "
    "WHERE state IN ('queued','offered')",
    "CREATE TABLE IF NOT EXISTS broker_bindings (lease_id TEXT PRIMARY KEY, "
    "binding TEXT NOT NULL, heartbeat_mono REAL NOT NULL, expiry_mono REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_reconcile (lease_id TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS broker_ticket_wait (ticket_id TEXT PRIMARY KEY, "
    "purpose TEXT NOT NULL, last_activity REAL NOT NULL, binding TEXT)",
    "CREATE TABLE IF NOT EXISTS broker_parked_runs (ticket_id TEXT NOT NULL, run_id TEXT NOT NULL, "
    "PRIMARY KEY(ticket_id, run_id))",
    "CREATE TABLE IF NOT EXISTS broker_claim_latency (ticket_id TEXT PRIMARY KEY, "
    "offer_sequence INTEGER NOT NULL, readback_at REAL NOT NULL, claimed_at REAL NOT NULL, "
    "latency_seconds REAL NOT NULL, run_id TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_offer_clock (ticket_id TEXT PRIMARY KEY, expires_mono REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_context (lease_id TEXT PRIMARY KEY, agent TEXT NOT NULL, issue TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, lease_id TEXT NOT NULL, change TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_verifications (lease_id TEXT PRIMARY KEY, verifier TEXT NOT NULL, evidence TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_recovery_deadlines (lease_id TEXT PRIMARY KEY, deadline_mono REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS broker_ticket_origins (ticket_id TEXT PRIMARY KEY, run_id TEXT NOT NULL)",
)


def process_identity(pid):
    """Linux process identity, including zombie rejection. Never called under a writer lock."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ProcessLookupError(pid)
    return {
        "mode": "local",
        "pid": pid,
        "process_start": fields[19],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def local_run_alive(binding):
    try:
        observed = process_identity(binding["pid"])
        return all(observed[k] == binding[k] for k in observed)
    except (OSError, ValueError, IndexError):
        return False


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
        monotonic=time.monotonic,
        run_probe=local_run_alive,
        admission_sources=None,
        recovery_io=None,
        recovery_owner=None,
        ops_issue=None,
        admission_guard=nullcontext,
    ):
        self.db_path = Path(db_path)
        self.registry = registry
        self.admitted = dict(admitted or {})
        self.clock, self.boundary = clock, boundary
        # Optional additional pure SQL conflicts, alongside this broker's durable queue.
        self.conflicting_waiter = conflicting_waiter
        self.draining = False
        self.monotonic, self.run_probe = monotonic, run_probe
        self._last_clock = None
        self.admission_sources = dict(admission_sources or {})
        self._admission = threading.local()
        self.recovery_io = recovery_io
        self.recovery_owner = recovery_owner
        self.ops_issue = ops_issue
        # The owner supplies one guard shared with claim writers; never a client option.
        self.admission_guard = admission_guard
        if recovery_io is not None:
            self._text(recovery_owner, ops_issue)

    def _owned(self, conn, lease_id, principal, run_id):
        group = self._view(conn, lease_id)
        if (group["holder"], group["run_id"]) != (principal, run_id):
            raise Refused("lease_not_yours")
        return group

    def status(self, *, lease_id, principal, run_id):
        with self.decision("status", locals()) as (conn, now, entry):
            self._text(lease_id, principal, run_id)
            self._expire(conn, now)
            return self._decide(entry, self._owned(conn, lease_id, principal, run_id))

    def renew(self, *, lease_id, principal, run_id, ttl=1800, idempotency_key):
        with self.decision("renew", locals()) as (conn, now, entry):
            self._text(lease_id, principal, run_id, idempotency_key)
            self._ttl(ttl)
            fingerprint = json.dumps(["renew", lease_id, ttl])
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            replay = self._replay(conn, principal, run_id, idempotency_key, fingerprint)
            if replay is not None:
                return self._decide(entry, replay)
            if now >= group["expires_at"]:
                raise Refused("lease_expired")
            if group["state"] != "active":
                raise Refused("lease_not_active")
            if self.draining:
                raise Refused("draining")
            if self._admission.unavailable or not self._admission.audit_ok:
                raise Refused("admission_unavailable")
            if group["expires_at"] - now > group["ttl"] / 3:
                raise Refused("renew_too_early")
            ids = [d["device_id"] for d in group["devices"]]
            if any(self.registry.resolve(d) is None for d in ids):
                raise Refused("device_unknown")
            maximum = 1800 if any(self.registry.resolve(d).pool == "root" for d in ids) else 3600
            self._ttl(ttl, maximum)
            entry["snapshot"]["conflicting_waiter"] = bool(
                self.conflicting_waiter(conn, frozenset(ids)) or any(
                    set(ids) & set(self._candidates(t["pool"], t["device_id"]))
                    for t in self._tickets(conn)
                )
            )
            if entry["snapshot"]["conflicting_waiter"]:
                raise Refused("conflicting_waiter")
            expiry = min(now + ttl, group["hard_deadline"])
            conn.execute(
                "UPDATE broker_groups SET expires_at=?, ttl=? WHERE lease_id=?",
                (expiry, ttl, lease_id),
            )
            conn.execute(
                "UPDATE broker_bindings SET expiry_mono=? WHERE lease_id=?",
                (self._mono + expiry - now, lease_id),
            )
            self.boundary("renew:updated")
            response = self._view(conn, lease_id)
            self._event(conn, "renew", now, response)
            self._record(conn, principal, run_id, idempotency_key, fingerprint, response)
            self._decide(entry, response)
        self.boundary("renew:committed")
        return response

    def release(self, *, lease_id, principal, run_id):
        with self.decision("release", locals(), allow_audit_failure=True) as (conn, now, entry):
            self._text(lease_id, principal, run_id)
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            if group["state"] == "active":
                self._event(conn, "release", now, group)
                conn.execute(
                    "UPDATE broker_groups SET state='fencing' WHERE lease_id=?", (lease_id,)
                )
            return self._decide(entry, self._view(conn, lease_id))

    def recovery(self, lease_id, expected, target, *, recovery_owner=None, ops_issue=None):
        """Trusted recovery-worker CAS, never a holder RPC. No automatic free path.

        Only the trusted recovery worker calls this after its actions succeed.
        Even 'verifying' is unavailable and cannot be client-freed.
        """
        with self.decision("recovery", locals(), allow_audit_failure=True) as (conn, now, entry):
            allowed = {("fencing", "cleaning"), ("cleaning", "verifying")}
            if target == "quarantine" and expected in {"fencing", "cleaning", "verifying"}:
                if not recovery_owner or not ops_issue:
                    raise Refused("recovery_owner_required")
                self._text(recovery_owner, ops_issue)
            elif (expected, target) not in allowed:
                raise Refused("invalid_transition")
            self._expire(conn, now)
            group = self._view(conn, lease_id)
            if group["state"] != expected:
                raise Refused("state_conflict")
            conn.execute(
                "UPDATE broker_groups SET state=?, recovery_owner=?, ops_issue=? WHERE lease_id=?",
                (target, recovery_owner, ops_issue, lease_id),
            )
            return self._decide(entry, self._view(conn, lease_id))

    @staticmethod
    def _decide(entry, response):
        entry["broker"] = response
        return response

    @contextmanager
    def decision(self, operation, arguments, *, allow_audit_failure=False):
        """Journal the committed outcome and its inputs, never invoke the shadow."""
        with self.transaction(allow_audit_failure=allow_audit_failure) as (conn, now):
            # ponytail: full development snapshots; bound retention before live rollout.
            inventory = {d.device_id: asdict(d) for d in self.registry._aliases.values()}
            entry = {
                "version": 1,
                "time_ict": datetime.fromtimestamp(now, timezone(timedelta(hours=7))).strftime(
                    "%Y-%m-%d %H:%M:%S ICT"
                ),
                "operation": operation,
                "request": {k: v for k, v in arguments.items() if k != "self"},
                "snapshot": {
                    "now": now,
                    "draining": self.draining,
                    "admitted": dict(self.admitted),
                    "inventory": list(inventory.values()),
                    "groups": [
                        self._view(conn, r[0])
                        for r in conn.execute(
                            "SELECT lease_id FROM broker_groups ORDER BY lease_id"
                        )
                    ],
                    "runs": [dict(r) for r in conn.execute("SELECT * FROM broker_runs")],
                    "requests": [dict(r) for r in conn.execute("SELECT * FROM broker_requests")],
                    "conflicting_waiter": False,
                },
            }
            try:
                yield conn, now, entry
            except Refused as exc:
                entry["broker"] = {"code": exc.code, **exc.details}
                raise
            finally:
                # Unexpected errors roll back both mutations and this journal entry.
                if "broker" in entry:
                    conn.execute(
                        "INSERT INTO broker_decisions(entry) VALUES (?)", (json.dumps(entry),)
                    )

    def restart(self):
        """Owner-only startup barrier, called after taking the daemon's exclusive lock."""
        with self.transaction(allow_audit_failure=True) as (conn, now):
            self._fence_all(conn)
            conn.execute(
                "UPDATE broker_recovery_deadlines SET deadline_mono=?", (self._mono + 120,)
            )

    def _fence_all(self, conn):
        conn.execute(
            "INSERT OR IGNORE INTO broker_reconcile SELECT lease_id "
            "FROM broker_groups WHERE state != 'released' AND state != 'quarantine'"
        )
        conn.execute("UPDATE broker_groups SET state='fencing' WHERE state='active'")
        # Restart/clock discontinuity invalidates signals; FIFO and sequence numbers survive.
        conn.execute(
            "UPDATE broker_tickets SET state='queued', reserved='[]', "
            "offer_expires_at=NULL WHERE state='offered'"
        )
        conn.execute("DELETE FROM broker_offer_clock")

    def finish_reconcile(self, lease_id):
        """Recovery acknowledgment, not release verification and never a holder RPC."""
        with self.transaction(allow_audit_failure=True) as (conn, now):
            group = self._view(conn, lease_id)
            if group["state"] not in {"verifying", "quarantine", "released"}:
                raise Refused("reconcile_incomplete")
            conn.execute("DELETE FROM broker_reconcile WHERE lease_id=?", (lease_id,))

    def heartbeat(self, *, lease_id, principal, run_id):
        self._text(lease_id, principal, run_id)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            if group["state"] != "active":
                raise Refused("lease_not_active")
            conn.execute(
                "UPDATE broker_bindings SET heartbeat_mono=? WHERE lease_id=?",
                (self._mono, lease_id),
            )
            return self._view(conn, lease_id)

    def maintain(self):
        """Periodic foreground maintenance; fencing records never free ownership."""
        with self.transaction(allow_audit_failure=True) as (conn, now):
            self._expire(conn, now)
        if self.recovery_io is not None:
            from recovery import recover_pending

            recover_pending(self)

    def _maintenance(self, conn, now, observations):
        self._mono = self.monotonic()
        if self._last_clock is not None:
            wall, mono = self._last_clock
            if self._mono < mono or abs((now - wall) - (self._mono - mono)) > 5:
                self._fence_all(conn)
        # Legacy queued tickets receive an initial activity sample on first upgrade.
        conn.execute(
            "INSERT OR IGNORE INTO broker_ticket_wait SELECT ticket_id, issue_id, ?, NULL "
            "FROM broker_tickets WHERE state IN ('queued','offered')",
            (now,),
        )
        for row in conn.execute(
            "SELECT ticket_id, binding FROM broker_ticket_wait WHERE binding IS NOT NULL"
        ):
            if observations.get((row["ticket_id"], row["binding"])) is False:
                self._park_binding(conn, row["ticket_id"], json.loads(row["binding"])["run_id"])
        for row in conn.execute("SELECT * FROM broker_bindings"):
            binding = json.loads(row["binding"])
            dead = (
                binding["mode"] == "local"
                and observations.get((row["lease_id"], row["binding"])) is False
            )
            suspect = binding["mode"] == "remote" and self._mono - row["heartbeat_mono"] >= 90
            if dead or suspect or self._mono >= row["expiry_mono"]:
                group = self._view(conn, row["lease_id"])
                if group["state"] == "active":
                    self._event(
                        conn,
                        "expire" if self._mono >= row["expiry_mono"] else "suspect",
                        now,
                        group,
                    )
                conn.execute(
                    "UPDATE broker_groups SET state='fencing' WHERE lease_id=? AND state='active'",
                    (row["lease_id"],),
                )

    @contextmanager
    def transaction(self, *, allow_audit_failure=False):
        with self.admission_guard():
            with self._transaction(allow_audit_failure=allow_audit_failure) as value:
                yield value

    @contextmanager
    def _transaction(self, *, allow_audit_failure=False):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            for statement in _DDL:
                conn.execute(statement)
            self._admission.audit_ok = True
            try:
                audit.flush(self.db_path)
            except (OSError, RuntimeError, UnicodeError):
                self._admission.audit_ok = False
            # Trusted readers only. Failure/unknown identity closes admission; no stale cache.
            blocked = set()
            self._admission.unavailable = (
                not {"artemis", "android"} <= self.admission_sources.keys()
                or self.admission_guard is nullcontext
            )
            try:
                for reader in self.admission_sources.values():
                    aliases = reader()
                    if not isinstance(aliases, (list, tuple, set, frozenset)):
                        raise ValueError("invalid claim snapshot")
                    for alias in aliases:
                        canonical = self._canonical(alias)
                        if canonical is None:
                            raise ValueError("unknown claim identity")
                        blocked.add(canonical)
            except Exception:
                self._admission.unavailable = True
            if self._admission.unavailable:
                blocked = set(self.admitted)
            self._admission.blocked = blocked
            # Process and boot-id reads happen before BEGIN IMMEDIATE, never under its lock.
            observations = {}
            for row in conn.execute(
                "SELECT b.* FROM broker_bindings b JOIN broker_groups g "
                "ON b.lease_id=g.lease_id WHERE g.state='active'"
            ).fetchall():
                binding = json.loads(row["binding"])
                if binding["mode"] == "local":
                    observations[row["lease_id"], row["binding"]] = self.run_probe(binding)
            for row in conn.execute(
                "SELECT w.ticket_id, w.binding FROM broker_ticket_wait w JOIN broker_tickets t "
                "ON w.ticket_id=t.ticket_id WHERE t.state IN ('queued','offered') AND w.binding IS NOT NULL"
            ).fetchall():
                binding = json.loads(row["binding"])
                if binding["mode"] == "local":
                    observations[row["ticket_id"], row["binding"]] = self.run_probe(binding)
            try:
                with write_transaction(conn, self.clock, refusals=(Refused,)) as value:
                    before = self._audit_snapshot(conn)
                    try:
                        self._maintenance(conn, value[1], observations)
                        yield value
                    finally:
                        self._audit_changes(conn, value[1], before)
            except Refused:
                # Refusals commit maintenance; interruptions must retain the old clock sample.
                self._last_clock = value[1], self._mono
                raise
            else:
                self._last_clock = value[1], self._mono
        finally:
            conn.close()
            # Includes committed maintenance on a domain refusal; rolled-back events vanish.
            try:
                audit.flush(self.db_path)
            except (OSError, RuntimeError, UnicodeError):
                if not allow_audit_failure:
                    raise

    @staticmethod
    def _audit_snapshot(conn):
        return (
            {r["lease_id"]: dict(r) for r in conn.execute("SELECT * FROM broker_groups")},
            {r["ticket_id"]: dict(r) for r in conn.execute("SELECT * FROM broker_tickets")},
        )

    def _event(self, conn, event, now, group, **details):
        context = conn.execute(
            "SELECT agent, issue FROM broker_context WHERE lease_id=?", (group["lease_id"],)
        ).fetchone()
        audit.enqueue(
            conn,
            event,
            now,
            agent=context["agent"] if context else group["holder"],
            issue=context["issue"] if context else group["purpose"],
            run_id=group["run_id"],
            devices=[d["device_id"] for d in group["devices"]],
            lease_id=group["lease_id"],
            **details,
        )

    def _audit_changes(self, conn, now, before):
        groups, tickets = self._audit_snapshot(conn)
        names = {
            "fencing": "fence",
            "cleaning": "cleanup",
            "verifying": "verify_pending",
            "released": "verify",
            "quarantine": "quarantine",
        }
        for key, row in groups.items():
            old = before[0].get(key)
            event = (
                "acquire"
                if old is None
                else names.get(row["state"])
                if old["state"] != row["state"]
                else None
            )
            if event:
                if row["state"] == "fencing":
                    binding = conn.execute(
                        "SELECT expiry_mono,heartbeat_mono,binding FROM broker_bindings WHERE lease_id=?",
                        (key,),
                    ).fetchone()
                    start = min(self._mono, binding[0]) if binding else self._mono
                    if binding and json.loads(binding[2])["mode"] == "remote":
                        start = min(start, binding[1] + 90)
                    conn.execute(
                        "INSERT OR REPLACE INTO broker_recovery_deadlines VALUES (?,?)",
                        (key, start + 120),
                    )
                self._event(
                    conn,
                    event,
                    now,
                    self._view(conn, key),
                    recovery_owner=row["recovery_owner"],
                    ops_issue=row["ops_issue"],
                )
        for key, row in tickets.items():
            old = before[1].get(key)
            if old != row:
                origin = conn.execute(
                    "SELECT run_id FROM broker_ticket_origins WHERE ticket_id=?", (key,)
                ).fetchone()
                audit.enqueue(
                    conn,
                    "offer" if row["state"] == "offered" else "ticket",
                    now,
                    agent=row["agent_id"],
                    run_id=origin[0] if origin else "legacy-unknown",
                    issue=row["issue_id"],
                    devices=json.loads(row["reserved"])
                    or self._candidates(row["pool"], row["device_id"]),
                    ticket_id=key,
                    state=row["state"],
                    sequence=row["offer_sequence"],
                )

    def record_change(self, *, lease_id, principal, run_id, change):
        """Owner records the undo intent BEFORE mutation; no arbitrary command strings."""
        from recovery import validate_change

        validate_change(change)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            group = self._owned(conn, lease_id, principal, run_id)
            if group["state"] != "active":
                raise Refused("lease_not_active")
            if change["device_id"] not in {d["device_id"] for d in group["devices"]}:
                raise Refused("device_not_yours")
            conn.execute(
                "INSERT INTO broker_changes(lease_id,change) VALUES (?,?)",
                (lease_id, json.dumps(change)),
            )
            self._event(conn, "change", now, group, kind=change["kind"])

    def verify_release(self, lease_id, *, verifier, evidence, passed):
        """Trusted leader entry point, deliberately absent from holder RPC/CLI."""
        self._text(verifier, evidence)
        if verifier != "5cd75ce5-d323-4004-9779-7e2ccf16bde4":
            raise Refused("leader_required")
        if type(passed) is not bool:
            raise Refused("invalid_request")
        with self.transaction() as (conn, now):
            group = self._view(conn, lease_id)
            if group["state"] != "verifying":
                raise Refused("state_conflict")
            if not passed:
                self._text(self.recovery_owner, self.ops_issue)
            conn.execute(
                "INSERT OR REPLACE INTO broker_verifications VALUES (?,?,?)",
                (lease_id, verifier, evidence),
            )
            conn.execute(
                "UPDATE broker_groups SET state=?, recovery_owner=?, ops_issue=? WHERE lease_id=?",
                (
                    "released" if passed else "quarantine",
                    None if passed else self.recovery_owner,
                    None if passed else self.ops_issue,
                    lease_id,
                ),
            )
            self._event(
                conn,
                "leader_evidence",
                now,
                group,
                verifier=verifier,
                evidence=evidence,
                passed=passed,
            )
            conn.execute("DELETE FROM broker_reconcile WHERE lease_id=?", (lease_id,))
            return self._view(conn, lease_id)

    def revoke(self, lease_id, *, actor, role):
        """Trusted control-plane entry point. The caller authenticates role; no holder RPC."""
        self._text(actor)
        if role not in {"cheese", "ops"}:
            raise Refused("revoke_forbidden")
        with self.transaction() as (conn, now):
            group = self._view(conn, lease_id)
            if group["state"] == "active":
                self._event(conn, "revoke", now, group, actor=actor)
                conn.execute(
                    "UPDATE broker_groups SET state='fencing' WHERE lease_id=?", (lease_id,)
                )
            return self._view(conn, lease_id)

    def recover_quarantine(self, lease_id, *, recovery_owner, evidence):
        """Named owner supplies recovery evidence; leader verification still follows."""
        self._text(recovery_owner, evidence)
        with self.transaction() as (conn, now):
            group = self._view(conn, lease_id)
            if group["state"] != "quarantine" or group["recovery_owner"] != recovery_owner:
                raise Refused("recovery_owner_required")
            self._event(conn, "recovery_evidence", now, group, evidence=evidence)
            conn.execute("UPDATE broker_groups SET state='verifying' WHERE lease_id=?", (lease_id,))
            return self._view(conn, lease_id)

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
        for row in conn.execute(
            "SELECT lease_id FROM broker_groups WHERE state='active' AND expires_at <= ?", (now,)
        ).fetchall():
            self._event(conn, "expire", now, self._view(conn, row[0]))
        conn.execute(
            "UPDATE broker_groups SET state='fencing' WHERE state='active' AND expires_at <= ?",
            (now,),
        )
        self._schedule(conn, now)

    def _candidates(self, pool, target=None):
        ids = canonical_ids([target] if target else self.admitted, self._canonical)
        return [
            canonical
            for canonical in ids
            if self.registry.resolve(canonical).pool == pool
            and self.admitted.get(canonical)
            and self._canonical(self.admitted[canonical]) == canonical
            and self.registry.eligible(self.admitted[canonical], "device")
        ]

    @staticmethod
    def _tickets(conn):
        return conn.execute(
            "SELECT * FROM broker_tickets WHERE state IN ('queued','offered') "
            "ORDER BY position, ticket_id"
        ).fetchall()

    @staticmethod
    def _occupied(conn):
        return {
            r["device_id"]: r["lease_id"]
            for r in conn.execute(
                "SELECT d.device_id, d.lease_id FROM broker_devices d JOIN broker_groups g "
                "ON d.lease_id=g.lease_id WHERE g.state != 'released'"
            )
        }

    @staticmethod
    def _ticket_view(conn, ticket_id):
        row = conn.execute(
            "SELECT * FROM broker_tickets WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise Refused("ticket_unknown")
        result = dict(row)
        result["reserved"] = json.loads(result["reserved"])
        wait = conn.execute(
            "SELECT * FROM broker_ticket_wait WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        result["binding"] = json.loads(wait["binding"]) if wait and wait["binding"] else None
        result["last_activity"] = wait["last_activity"] if wait else None
        return result

    def _ticket_owned(self, conn, ticket_id, principal, agent_id, issue_id):
        ticket = self._ticket_view(conn, ticket_id)
        if (ticket["principal"], ticket["agent_id"], ticket["issue_id"]) != (
            principal,
            agent_id,
            issue_id,
        ):
            raise Refused("ticket_not_yours")
        return ticket

    def ticket_status(self, *, ticket_id, principal, agent_id, issue_id):
        self._text(ticket_id, principal, agent_id, issue_id)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            self._ticket_owned(conn, ticket_id, principal, agent_id, issue_id)
            conn.execute(
                "UPDATE broker_ticket_wait SET last_activity=? WHERE ticket_id=? "
                "AND EXISTS (SELECT 1 FROM broker_tickets WHERE ticket_id=? AND state IN ('queued','offered'))",
                (now, ticket_id, ticket_id),
            )
            return self._ticket_view(conn, ticket_id)

    @staticmethod
    def _park_binding(conn, ticket_id, run_id):
        conn.execute("INSERT OR IGNORE INTO broker_parked_runs VALUES (?,?)", (ticket_id, run_id))
        conn.execute("UPDATE broker_ticket_wait SET binding=NULL WHERE ticket_id=?", (ticket_id,))

    def park(self, *, ticket_id, principal, agent_id, issue_id, run_id):
        self._text(ticket_id, principal, agent_id, issue_id, run_id)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            ticket = self._ticket_owned(conn, ticket_id, principal, agent_id, issue_id)
            if ticket["state"] not in {"queued", "offered"}:
                raise Refused("ticket_closed")
            if ticket["binding"] and ticket["binding"]["run_id"] != run_id:
                raise Refused("run_not_yours")
            self._park_binding(conn, ticket_id, run_id)
            return self._ticket_view(conn, ticket_id)

    def claim(self, *, ticket_id, principal, agent_id, issue_id, run_id, binding, idempotency_key):
        self._text(ticket_id, principal, agent_id, issue_id, run_id, idempotency_key)
        if not isinstance(binding, dict):
            raise Refused("invalid_request")
        binding = self._binding(binding, run_id)
        fingerprint = json.dumps(["claim", ticket_id, agent_id, issue_id, binding], sort_keys=True)
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            replay = self._replay(conn, principal, run_id, idempotency_key, fingerprint)
            if replay is not None:
                return replay
            ticket = self._ticket_owned(conn, ticket_id, principal, agent_id, issue_id)
            if conn.execute(
                "SELECT 1 FROM broker_parked_runs WHERE ticket_id=? AND run_id=?",
                (ticket_id, run_id),
            ).fetchone():
                raise Refused("run_parked")
            if self.draining:
                raise Refused("draining")
            if conn.execute("SELECT 1 FROM broker_reconcile LIMIT 1").fetchone():
                raise Refused("restart_reconcile")
            if ticket["state"] != "offered":
                raise Refused("ticket_not_offered")
            if ticket["binding"] is not None:
                raise Refused("ticket_not_parked")
            purpose = conn.execute(
                "SELECT purpose FROM broker_ticket_wait WHERE ticket_id=?", (ticket_id,)
            ).fetchone()[0]
            response = self._grant(
                conn,
                now,
                principal,
                run_id,
                purpose,
                ticket["pool"],
                ticket["ttl"],
                ticket["reserved"],
                binding,
                agent_id=agent_id, issue_id=issue_id,
            )
            conn.execute(
                "UPDATE broker_tickets SET state='claimed', reserved='[]' WHERE ticket_id=?",
                (ticket_id,),
            )
            conn.execute(
                "UPDATE broker_ticket_wait SET last_activity=? WHERE ticket_id=?", (now, ticket_id)
            )
            conn.execute("DELETE FROM broker_offer_clock WHERE ticket_id=?", (ticket_id,))
            if ticket["offer_expires_at"] is not None:
                readback = ticket["offer_expires_at"] - 900
                conn.execute(
                    "INSERT INTO broker_claim_latency VALUES (?,?,?,?,?,?)",
                    (
                        ticket_id,
                        ticket["offer_sequence"],
                        readback,
                        now,
                        max(0, now - readback),
                        run_id,
                    ),
                )
            self._record(conn, principal, run_id, idempotency_key, fingerprint, response)
            self._schedule(conn, now)
        self.boundary("claim:committed")
        return response

    def pending_offers(self):
        """Owner-only snapshot; no poll refresh and no client property credential."""
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            return [
                dict(t)
                for t in self._tickets(conn)
                if t["state"] == "offered" and t["offer_expires_at"] is None
            ]

    def cancel_ticket(self, *, ticket_id, principal, agent_id, issue_id):
        self._text(ticket_id, principal, agent_id, issue_id)
        with self.transaction() as (conn, now):
            self._ticket_owned(conn, ticket_id, principal, agent_id, issue_id)
            conn.execute(
                "UPDATE broker_tickets SET state='cancelled', reserved='[]', "
                "offer_expires_at=NULL WHERE ticket_id=? AND state IN ('queued','offered')",
                (ticket_id,),
            )
            conn.execute("DELETE FROM broker_offer_clock WHERE ticket_id=?", (ticket_id,))
            self._expire(conn, now)
            return self._ticket_view(conn, ticket_id)

    def start_offer_window(self, ticket_id, offer_sequence):
        """Trusted signal worker calls this only after matching property read-back."""
        self._text(ticket_id)
        if type(offer_sequence) is not int or offer_sequence < 1:
            raise Refused("invalid_request")
        with self.transaction() as (conn, now):
            self._expire(conn, now)
            ticket = self._ticket_view(conn, ticket_id)
            if ticket["state"] != "offered" or ticket["offer_sequence"] != offer_sequence:
                raise Refused("offer_stale")
            if ticket["offer_expires_at"] is None:
                conn.execute(
                    "INSERT OR REPLACE INTO broker_offer_clock VALUES (?,?)",
                    (ticket_id, self._mono + 900),
                )
            conn.execute(
                "UPDATE broker_tickets SET offer_expires_at=? "
                "WHERE ticket_id=? AND offer_expires_at IS NULL",
                (now + 900, ticket_id),
            )
            return self._ticket_view(conn, ticket_id)

    def _schedule(self, conn, now):
        conn.execute(
            "UPDATE broker_tickets SET state='cancelled', reserved='[]', offer_expires_at=NULL "
            "WHERE state IN ('queued','offered') AND ticket_id IN "
            "(SELECT ticket_id FROM broker_ticket_wait WHERE last_activity <= ?)",
            (now - 4500,),
        )
        conn.execute(
            "DELETE FROM broker_offer_clock WHERE ticket_id IN "
            "(SELECT ticket_id FROM broker_tickets WHERE state='cancelled')"
        )
        if self.draining or conn.execute("SELECT 1 FROM broker_reconcile LIMIT 1").fetchone():
            return
        for ticket in self._tickets(conn):
            deadline = conn.execute(
                "SELECT expires_mono FROM broker_offer_clock WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()
            if ticket["offer_expires_at"] is not None and (
                ticket["offer_expires_at"] <= now or (deadline and self._mono >= deadline[0])
            ):
                tail = conn.execute(
                    "SELECT coalesce(max(position),0)+1 FROM broker_tickets"
                ).fetchone()[0]
                conn.execute(
                    "UPDATE broker_tickets SET missed_offers=missed_offers+1, "
                    "state=CASE WHEN missed_offers+1 >= 2 THEN 'cancelled' ELSE 'queued' END, "
                    "position=?, reserved='[]', offer_expires_at=NULL WHERE ticket_id=?",
                    (tail, ticket["ticket_id"]),
                )
                conn.execute(
                    "DELETE FROM broker_offer_clock WHERE ticket_id=?", (ticket["ticket_id"],)
                )
        occupied = self._occupied(conn)
        blocked = set()
        # ponytail: linear queue scan; index conflict sets if measured queue size requires it.
        for ticket in self._tickets(conn):
            candidates = self._candidates(ticket["pool"], ticket["device_id"])
            conflict = bool(blocked.intersection(candidates))
            blocked.update(candidates)
            free = (
                [d for d in candidates if d not in occupied and d not in self._admission.blocked]
                if not conflict
                else []
            )
            previous = json.loads(ticket["reserved"])
            reserved = (
                previous
                if ticket["state"] == "offered" and set(previous) <= set(free)
                else free[: ticket["count"]]
            )
            offered = len(reserved) == ticket["count"]
            state = "offered" if offered else "queued"
            new_offer = offered and (ticket["state"] != "offered" or previous != reserved)
            sequence = ticket["offer_sequence"] + new_offer
            if new_offer or not offered:
                conn.execute(
                    "DELETE FROM broker_offer_clock WHERE ticket_id=?", (ticket["ticket_id"],)
                )
            conn.execute(
                "UPDATE broker_tickets SET state=?, reserved=?, offer_sequence=?, "
                "offer_expires_at=CASE WHEN ? THEN offer_expires_at ELSE NULL END WHERE ticket_id=?",
                (
                    state,
                    json.dumps(reserved),
                    sequence,
                    offered and not new_offer,
                    ticket["ticket_id"],
                ),
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
        binding = conn.execute(
            "SELECT binding FROM broker_bindings WHERE lease_id=?", (lease_id,)
        ).fetchone()
        group["binding"] = json.loads(binding[0]) if binding else None
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
        agent_id=None,
        issue_id=None,
        binding=None,
    ):
        agent_id = principal if agent_id is None else agent_id
        issue_id = purpose if issue_id is None else issue_id
        self._text(principal, run_id, purpose, pool, idempotency_key)
        self._text(agent_id, issue_id)
        self._ttl(ttl, 1800 if pool == "root" else 3600)
        if type(count) is not int or count < 1:
            raise Refused("invalid_request")
        if device_id is not None:
            self._text(device_id)
        binding = self._binding(binding, run_id)
        with self.decision("acquire", locals()) as (conn, now, entry):
            target = self._canonical(device_id) if device_id else None
            fingerprint = json.dumps(
                [
                    "acquire",
                    pool,
                    target or device_id,
                    count,
                    ttl,
                    purpose,
                    agent_id,
                    issue_id,
                    binding,
                ],
                sort_keys=True,
            )
            self._expire(conn, now)
            replay = self._replay(conn, principal, run_id, idempotency_key, fingerprint)
            if replay is not None:
                return self._decide(entry, replay)
            if self.draining:
                raise Refused("draining")
            if self._admission.unavailable or not self._admission.audit_ok:
                raise Refused("admission_unavailable")
            if conn.execute("SELECT 1 FROM broker_reconcile LIMIT 1").fetchone():
                raise Refused("restart_reconcile")
            existing = conn.execute(
                "SELECT ticket_id FROM broker_tickets WHERE issue_id=? "
                "AND state IN ('queued','offered')",
                (issue_id,),
            ).fetchone()
            if existing:
                ticket = self._ticket_owned(conn, existing[0], principal, agent_id, issue_id)
                occupied = self._occupied(conn)
                holders = [
                    self._view(conn, occupied[d])
                    for d in self._candidates(ticket["pool"], ticket["device_id"])
                    if d in occupied
                ]
                raise Refused(
                    "device_busy",
                    ticket=ticket,
                    holder=min(holders, key=lambda g: g["expires_at"]) if holders else None,
                )
            candidates = self._candidates(pool, target or device_id)
            if count > len(candidates):
                raise Refused("impossible_batch")
            self._run_budget(conn, now, principal, run_id, pool)
            occupied = self._occupied(conn)
            free = [d for d in candidates if d not in occupied and d not in self._admission.blocked]
            conflict = any(
                set(candidates) & set(self._candidates(t["pool"], t["device_id"]))
                for t in self._tickets(conn)
            )
            if len(free) < count or conflict:
                ticket_id = uuid.uuid4().hex
                conn.execute("INSERT INTO broker_ticket_origins VALUES (?,?)", (ticket_id, run_id))
                position = conn.execute(
                    "SELECT coalesce(max(position),0)+1 FROM broker_tickets"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO broker_tickets "
                    "(ticket_id, principal, agent_id, issue_id, pool, device_id, count, ttl, position, state) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'queued')",
                    (
                        ticket_id,
                        principal,
                        agent_id,
                        issue_id,
                        pool,
                        target or device_id,
                        count,
                        ttl,
                        position,
                    ),
                )
                conn.execute(
                    "INSERT INTO broker_ticket_wait VALUES (?,?,?,?)",
                    (ticket_id, purpose, now, json.dumps(binding, sort_keys=True)),
                )
                self._schedule(conn, now)
                holders = [self._view(conn, occupied[d]) for d in candidates if d in occupied]
                raise Refused(
                    "device_busy",
                    ticket=self._ticket_view(conn, ticket_id),
                    holder=min(holders, key=lambda g: g["expires_at"]) if holders else None,
                )
            response = self._grant(
                conn, now, principal, run_id, purpose, pool, ttl, free[:count], binding, agent_id=agent_id, issue_id=issue_id
            )
            self._record(conn, principal, run_id, idempotency_key, fingerprint, response)
            self._decide(entry, response)
        self.boundary("acquire:committed")
        return response

    def _binding(self, binding, run_id):
        binding = (
            dict(binding)
            if isinstance(binding, dict)
            else process_identity(os.getpid())
            if binding is None
            else {}
        )
        if binding.get("mode") == "local":
            if (
                set(binding) != {"mode", "pid", "process_start", "boot_id"}
                or type(binding["pid"]) is not int
                or binding["pid"] < 1
            ):
                raise Refused("invalid_request")
            self._text(binding["process_start"], binding["boot_id"])
            if not self.run_probe(binding):
                raise Refused("run_not_live")
        elif binding != {"mode": "remote"}:
            raise Refused("invalid_request")
        binding["run_id"] = run_id
        return binding

    def _run_budget(self, conn, now, principal, run_id, pool):
        held = conn.execute(
            "SELECT lease_id FROM broker_groups WHERE principal=? "
            "AND run_id=? AND state != 'released'",
            (principal, run_id),
        ).fetchone()
        if held:
            raise Refused("run_already_holds")
        budget = conn.execute(
            "SELECT * FROM broker_runs WHERE principal=? AND run_id=?", (principal, run_id)
        ).fetchone()
        started = budget["started_at"] if budget else now
        cap = 3600 if pool == "root" else 7200
        deadline = min(started + cap, budget["hard_deadline"]) if budget else now + cap
        if now >= deadline:
            raise Refused("hard_cap")
        return started, deadline

    def _grant(self, conn, now, principal, run_id, purpose, pool, ttl, devices, binding, *, agent_id=None, issue_id=None):
        if self._admission.unavailable or not self._admission.audit_ok:
            raise Refused("admission_unavailable")
        if set(devices) & self._admission.blocked:
            raise Refused("admission_unavailable")
        started, deadline = self._run_budget(conn, now, principal, run_id, pool)
        lease_id = uuid.uuid4().hex
        conn.execute("INSERT INTO broker_context VALUES (?,?,?)",
                     (lease_id, agent_id or principal, issue_id or purpose))
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
        for device_id in devices:
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
        conn.execute(
            "INSERT INTO broker_bindings VALUES (?,?,?,?)",
            (
                lease_id,
                json.dumps(binding, sort_keys=True),
                self._mono,
                self._mono + min(ttl, deadline - now),
            ),
        )
        response = self._view(conn, lease_id)
        return response
