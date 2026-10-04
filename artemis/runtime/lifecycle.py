# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The single owner of run outcomes (CHE-1089).

Every terminal session transition (completed, failed, cancelled, interrupted)
goes through :class:`LifecycleAuthority`. One SQLite ``BEGIN IMMEDIATE``
transaction reads the session, arbitrates the requested outcome, writes the
``sessions`` row and inserts one outbox row, so the first committed outcome is
final and is announced exactly once. The ``status.json`` projection is
derived from the committed row, never written independently.

Precedence between outcomes that compete before commit:
cancelled > completed > interrupted > failed. A committed terminal outcome is
immutable, so a valid completion or cancellation always beats a later loss.
The worker exit cause is stored beside the outcome, never as the outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from artemis.runtime import trace_store

logger = logging.getLogger(__name__)


class InterruptReason(StrEnum):
    HOST_DISCONNECTED = "host_disconnected"
    BRIDGE_CLOSED = "bridge_closed"
    DEVICE_OFFLINE = "device_offline"
    SERVER_RESTARTED = "server_restarted"
    AUTH_EXPIRED = "auth_expired"


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
# Higher wins when outcomes compete before commit.
_PROJECT_ATTEMPTS = 3
_PRECEDENCE = {"failed": 1, "interrupted": 2, "completed": 3, "cancelled": 4}

_SCHEMA_COLUMNS = (
    ("interrupt_reason", "TEXT"),
    ("exit_cause", "TEXT"),
    ("pending_loss_reason", "TEXT"),
)
_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS lifecycle_outbox (
    dedupe_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    interrupt_reason TEXT,
    created_at REAL NOT NULL,
    delivered_at REAL
)
"""


def canonical_status(status: str | None) -> str | None:
    """Normalize the legacy ``success`` alias; every consumer sees ``completed``."""
    if not isinstance(status, str):
        return None
    normalized = status.lower().strip()
    return "completed" if normalized == "success" else normalized


def can_transition(current: str | None, new: str) -> bool:
    """A committed terminal outcome is immutable; anything else may move on."""
    current = canonical_status(current)
    new = canonical_status(new)
    return current not in TERMINAL_STATUSES and new != current


def resolve_outcome(
    requested: str,
    reason: InterruptReason | None,
    pending_loss: InterruptReason | None,
) -> tuple[str, InterruptReason | None]:
    """Pick the winning outcome between a requested one and a recorded loss.

    A loss noted earlier turns a later worker failure into ``interrupted``
    (the worker died because the link did), but never overrides a completion
    or cancellation.
    """
    candidates: list[tuple[str, InterruptReason | None]] = [(requested, reason)]
    if pending_loss is not None:
        candidates.append(("interrupted", pending_loss))
    return max(candidates, key=lambda c: _PRECEDENCE[c[0]])


def ensure_lifecycle_schema(conn: sqlite3.Connection) -> bool:
    """Idempotent additive migration: lifecycle columns plus the outbox table.

    Returns False (and changes nothing) when the sessions table does not exist yet.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if not existing:
        return False
    for name, decl in _SCHEMA_COLUMNS:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                pass  # another process added it first
    conn.execute(_OUTBOX_DDL)
    conn.commit()
    return True


@dataclass(frozen=True, slots=True)
class Outcome:
    session_id: str
    status: str | None
    interrupt_reason: InterruptReason | None = None
    # True only for the call whose transaction published the outcome.
    committed: bool = False


def _reason(value: Any) -> InterruptReason | None:
    return InterruptReason(value) if value else None


class LifecycleAuthority:
    """Transactional owner of session outcomes for one sessions database."""

    _schema_ready: set[str] = set()
    _schema_lock = threading.Lock()

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        pause: Callable[[str], None] | None = None,
    ) -> None:
        if db_path is None:
            from artemis.config import DB_PATH

            db_path = DB_PATH
        self.db_path = Path(db_path)
        self._clock = clock
        # Test seam: called at named points inside the open transaction so an
        # interleaving test can hold one writer while another one arrives.
        self._pause = pause or (lambda point: None)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_schema(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        key = str(self.db_path)
        with self._schema_lock:
            if key in self._schema_ready:
                return
            if ensure_lifecycle_schema(conn):
                self._schema_ready.add(key)

    # -- commits ---------------------------------------------------------

    def finish(
        self,
        session_id: str,
        status: str,
        *,
        reason: InterruptReason | str | None = None,
        error: str | None = None,
        exit_cause: str | None = None,
        end_time: float | None = None,
    ) -> Outcome:
        """Commit ``status`` unless the session already has a final outcome."""
        session_id = str(session_id)
        status = canonical_status(status) or ""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"{status!r} is not a terminal session status")
        reason = _reason(reason)
        if (status == "interrupted") != (reason is not None):
            raise ValueError("interrupt reason is required for, and only for, interrupted")

        with self._txn() as conn:
            row = conn.execute(
                "SELECT status, interrupt_reason, pending_loss_reason FROM sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return Outcome(session_id, None)
            self._pause("after_read")
            if exit_cause is not None:
                conn.execute(
                    "UPDATE sessions SET exit_cause = COALESCE(exit_cause, ?) WHERE session_id = ?",
                    (exit_cause, session_id),
                )
            current = canonical_status(row["status"])
            if can_transition(current, status):
                final, final_reason = resolve_outcome(
                    status, reason, _reason(row["pending_loss_reason"])
                )
                self._commit(conn, session_id, final, final_reason, end_time or self._clock())
                self._pause("before_commit")
                outcome = Outcome(session_id, final, final_reason, committed=True)
            else:
                outcome = Outcome(session_id, current, _reason(row["interrupt_reason"]))
        # Project after commit. A crash or write failure here leaves status.json
        # stale; any later finish() for the session re-projects from the row.
        if outcome.status in TERMINAL_STATUSES:
            self.project(session_id, error=error if outcome.committed else None)
        return outcome

    @staticmethod
    def _commit(
        conn: sqlite3.Connection,
        session_id: str,
        status: str,
        reason: InterruptReason | None,
        now: float,
    ) -> None:
        reason_value = reason.value if reason else None
        conn.execute(
            "UPDATE sessions SET status = ?, end_time = ?, interrupt_reason = ?, "
            "pending_loss_reason = NULL WHERE session_id = ?",
            (status, now, reason_value, session_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO lifecycle_outbox "
            "(dedupe_id, session_id, status, interrupt_reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"{session_id}:outcome", session_id, status, reason_value, now),
        )

    def interrupt(
        self, session_id: str, reason: InterruptReason | str, *, error: str | None = None
    ) -> Outcome:
        return self.finish(session_id, "interrupted", reason=reason, error=error)

    def note_loss(self, session_id: str, reason: InterruptReason | str) -> bool:
        """Record a loss without publishing; a later worker failure becomes interrupted."""
        reason = InterruptReason(reason)
        with self._txn() as conn:
            cur = conn.execute(
                "UPDATE sessions SET pending_loss_reason = ? WHERE session_id = ? "
                "AND pending_loss_reason IS NULL "
                "AND COALESCE(status, 'running') NOT IN ('completed','failed','cancelled',"
                "'interrupted','success')",
                (reason.value, str(session_id)),
            )
            return cur.rowcount > 0

    def settle_worker_exit(self, session_id: str, returncode: int, manual_stop: bool) -> Outcome:
        """Fallback finalizer after the worker process exits; never overrides a result."""
        if manual_stop:
            status = "cancelled"
        else:
            status = "completed" if returncode == 0 else "failed"
        return self.finish(session_id, status, exit_cause=f"exit:{returncode}")

    def interrupt_running_after_restart(self, is_alive: Callable[[Any], bool]) -> list[str]:
        """Server (re)start: running sessions whose worker is gone are interrupted."""
        return self._finish_dead_running(
            is_alive, "interrupted", InterruptReason.SERVER_RESTARTED, "server_restarted"
        )

    def fail_vanished_workers(
        self, is_alive: Callable[[Any], bool], skip: Callable[[str], bool]
    ) -> list[str]:
        """Periodic sweep: running sessions nobody owns and whose worker died failed."""
        return self._finish_dead_running(is_alive, "failed", None, "worker_vanished", skip)

    def _finish_dead_running(
        self,
        is_alive: Callable[[Any], bool],
        status: str,
        reason: InterruptReason | None,
        exit_cause: str,
        skip: Callable[[str], bool] = lambda sid: False,
    ) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT session_id, pid FROM sessions WHERE status = 'running'")
            candidates = [(r["session_id"], r["pid"]) for r in rows.fetchall()]
        finished: list[str] = []
        for sid, pid in candidates:
            if skip(sid) or is_alive(pid):
                continue
            if self.finish(sid, status, reason=reason, exit_cause=exit_cause).committed:
                finished.append(sid)
        return finished

    # -- projection and delivery -----------------------------------------

    def project(self, session_id: str, *, error: str | None = None) -> None:
        """Mirror the committed outcome into status.json (when the trace has one)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, end_time, interrupt_reason FROM sessions WHERE session_id = ?",
                (str(session_id),),
            ).fetchone()
        status = canonical_status(row["status"]) if row else None
        if status not in TERMINAL_STATUSES:
            return
        for attempt in range(_PROJECT_ATTEMPTS):
            try:
                trace_store.publish_outcome(
                    str(session_id),
                    status,
                    end_time=row["end_time"],
                    error=error,
                    interrupt_reason=row["interrupt_reason"],
                )
                return
            except OSError as exc:
                # A stale status.json leaves MCP pollers seeing "running", so
                # retry transient write failures before giving up.
                if attempt == _PROJECT_ATTEMPTS - 1:
                    logger.error(
                        "Could not project outcome for %s into status.json after %d attempts: %s",
                        session_id,
                        attempt + 1,
                        exc,
                    )
                else:
                    time.sleep(0.5 * (attempt + 1))

    def claim_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return and mark delivered the undelivered outcome events, exactly once."""
        where, args = ("AND session_id = ?", (str(session_id),)) if session_id else ("", ())
        with self._txn() as conn:
            rows = conn.execute(
                f"SELECT dedupe_id, session_id, status, interrupt_reason, created_at "
                f"FROM lifecycle_outbox WHERE delivered_at IS NULL {where} ORDER BY created_at",
                args,
            ).fetchall()
            conn.executemany(
                "UPDATE lifecycle_outbox SET delivered_at = ? WHERE dedupe_id = ?",
                [(self._clock(), r["dedupe_id"]) for r in rows],
            )
            return [dict(r) for r in rows]
