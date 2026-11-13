"""Owner-only cutover controls. Never exposed over the holder socket or HTTP API.

The caller authenticates ops and verifies G4 approval before using these hooks.
Both authority implementations must share this database and admission guard.
"""

import sqlite3
import threading
from contextlib import contextmanager

from broker import Refused


class Authority:
    def __init__(self, broker, *, actor, run_id, issue):
        self.broker = broker
        broker._text(actor, run_id, issue)
        self.actor, self.run_id, self.issue = actor, run_id, issue
        with broker.transaction() as (conn, now):
            pass  # initialize private storage, without enabling any new authority

    def readback(self):
        with sqlite3.connect(self.broker.db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            result = self.broker._authority(conn)
            groups = [
                self.broker._view(conn, r[0])
                for r in conn.execute(
                    "SELECT lease_id FROM broker_groups WHERE state != 'released' ORDER BY lease_id"
                ).fetchall()
            ]
            result["quarantine"] = [g for g in groups if g["state"] == "quarantine"]
            result["unresolved"] = [g for g in groups if g["state"] != "quarantine"]
            return result

    @staticmethod
    def _expected(row, name, generation):
        if type(generation) is not int or generation < 1:
            raise Refused("invalid_request")
        if (row["name"], row["generation"]) != (name, generation):
            raise Refused("authority_stale")

    def _event(self, conn, now, action):
        import audit

        audit.enqueue(
            conn,
            action,
            now,
            agent=self.actor,
            run_id=self.run_id,
            issue=self.issue,
            devices=[],
            authority=self.broker._authority(conn),
        )

    def drain(self, name, generation):
        with self.broker.transaction() as (conn, now):
            row = self.broker._authority(conn)
            self._expected(row, name, generation)
            if row["state"] not in {"enabled", "draining"}:
                raise Refused("invalid_transition")
            conn.execute("UPDATE broker_authority SET state='draining' WHERE singleton=1")
            self._event(conn, now, "authority_drain")
        return self.readback()

    def fence(self, name, generation, successor, *, gate=None):
        self.broker._text(name, successor)
        if type(generation) is not int or generation < 1:
            raise Refused("invalid_request")
        if gate is not None and gate.authority is not self:
            raise Refused("gateway_mismatch")
        with self.broker.transaction(allow_audit_failure=True) as (conn, now):
            row = self.broker._authority(conn)
            # Retry the same committed bump after lost response or gateway failure.
            if (row["name"], row["generation"], row["state"]) != (
                successor,
                generation + 1,
                "fenced",
            ):
                self._expected(row, name, generation)
                if row["state"] != "draining":
                    raise Refused("authority_not_draining")
                self.broker._fence_all(conn)
                conn.execute(
                    "UPDATE broker_authority SET name=?, generation=generation+1, "
                    "state='fenced', fenced=0 WHERE singleton=1",
                    (successor,),
                )
                self._event(conn, now, "authority_fence")
        # No external stream close while the SQLite writer lock is held.
        if gate is not None:
            gate.reconcile()
            with self.broker.transaction(allow_audit_failure=True) as (conn, now):
                row = self.broker._authority(conn)
                self._expected(row, successor, generation + 1)
                if row["state"] != "fenced":
                    raise Refused("invalid_transition")
                conn.execute("UPDATE broker_authority SET fenced=1 WHERE singleton=1")
                self._event(conn, now, "authority_gateway_closed")
        return self.readback()

    def enable(self, name, generation):
        with self.broker.transaction() as (conn, now):
            row = self.broker._authority(conn)
            if row["state"] != "fenced" or row["fenced"] != 1:
                raise Refused("authority_not_fenced")
            self._expected(row, name, generation)
            if conn.execute(
                "SELECT 1 FROM broker_groups WHERE state NOT IN ('released','quarantine') LIMIT 1"
            ).fetchone():
                raise Refused("leases_unresolved")
            if not self.broker._admission.audit_ok:
                raise Refused("admission_unavailable")
            conn.execute("DELETE FROM broker_reconcile")
            conn.execute("UPDATE broker_authority SET state='enabled' WHERE singleton=1")
            self._event(conn, now, "authority_enable")
        return self.readback()


class GenerationGate:
    """Supplement the R1 authenticated credential check; this is not authentication.

    The single gateway owner tracks BOTH sides of every stream. Use the existing
    adb_gateway.Gateway parser with access=lambda: gate.valid(name, generation).
    Stream register/unregister and cutover reconcile share this lock.
    """

    def __init__(self, authority):
        self.authority = authority
        self.lock = threading.RLock()
        self.streams = {}

    def valid(self, name, generation):
        if type(generation) is not int or generation < 1:
            return False
        try:
            row = self.authority.readback()
            return (row["name"], row["generation"]) == (name, generation) and row["state"] in {
                "enabled",
                "draining",
            }
        except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
            return False

    def register(self, name, generation, stream):
        with self.lock:
            if not self.valid(name, generation):
                stream.close()
                raise Refused("authority_stale")
            self.streams[stream] = (name, generation)

    def unregister(self, stream):
        with self.lock:
            self.streams.pop(stream, None)

    def gateway(self, name, generation, shared):
        """R1 owner calls after authenticating the complete per-run credential."""
        from artemis.runtime.adb_gateway import Gateway

        return Gateway(
            shared,
            access=lambda: self.valid(name, generation),
            track=lambda *streams: self.track(name, generation, *streams),
        )

    @contextmanager
    def track(self, name, generation, *streams):
        keep = set()
        try:
            try:
                with self.lock:
                    for stream in streams:
                        self.register(name, generation, stream)
            except Exception:
                for stream in streams:
                    try:
                        stream.close()
                    except Exception:
                        # Retain uncertain closes so fence retries cannot forget a stream.
                        with self.lock:
                            self.streams[stream] = (name, generation)
                        keep.add(stream)
                raise
            yield
        finally:
            for stream in streams:
                if stream not in keep:
                    self.unregister(stream)

    def reconcile(self):
        with self.lock:
            failed = False
            for stream, (name, generation) in list(self.streams.items()):
                if not self.valid(name, generation):
                    try:
                        stream.close()
                        del self.streams[stream]
                    except Exception:
                        failed = True  # attempt every other close, retain failed handles
            if failed:
                raise Refused("gateway_fence_failed")
