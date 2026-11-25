"""Durable desired state for host recordings in the shared Artemis database."""

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from apps.admin_console.database.connection import get_db

RECORDING_CAP_SECONDS = 35 * 60
READY_POLL_SECONDS = 0.2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_recording_assignments (
    recording_id TEXT NOT NULL PRIMARY KEY,
    host_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    run_generation INTEGER NOT NULL,
    desired_state TEXT NOT NULL CHECK (desired_state IN ('record', 'stop')),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    acked_revision INTEGER NOT NULL CHECK (acked_revision BETWEEN 0 AND revision),
    deadline REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ready_at REAL,
    host_started_at REAL,
    clock_offset REAL
)
"""


@dataclass(frozen=True)
class RecordingAssignment:
    recording_id: str
    host_id: str
    session_id: str
    run_generation: int
    desired_state: Literal["record", "stop"]
    revision: int
    acked_revision: int
    deadline: float
    created_at: float
    updated_at: float
    ready_at: float | None = None
    host_started_at: float | None = None
    clock_offset: float | None = None

    @property
    def server_started_at(self) -> float | None:
        """The recording start in server time, once the host reported both values."""
        if self.host_started_at is None or self.clock_offset is None:
            return None
        return self.host_started_at + self.clock_offset

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> "RecordingAssignment":
        return cls(
            recording_id=row["recording_id"],
            host_id=row["host_id"],
            session_id=row["session_id"],
            run_generation=row["run_generation"],
            desired_state=row["desired_state"],
            revision=row["revision"],
            acked_revision=row["acked_revision"],
            deadline=row["deadline"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ready_at=row["ready_at"],
            host_started_at=row["host_started_at"],
            clock_offset=row["clock_offset"],
        )


class RecordingAssignmentStore:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db_path = db_path
        self.clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_SCHEMA)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(host_recording_assignments)")
            }
            for column in ("ready_at", "host_started_at", "clock_offset"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE host_recording_assignments ADD COLUMN {column} REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS host_recording_assignments_by_host "
                "ON host_recording_assignments (host_id, created_at, recording_id)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(get_db(self._db_path)) as conn, conn:
            yield conn

    def assign(self, host_id: str, session_id: str, generation: int) -> RecordingAssignment:
        """Create an assignment, or return the unchanged assignment for this session."""
        now = self.clock()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO host_recording_assignments (
                    recording_id, host_id, session_id, run_generation, desired_state,
                    revision, acked_revision, deadline, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'record', 1, 0, ?, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    uuid4().hex,
                    host_id,
                    session_id,
                    generation,
                    now + RECORDING_CAP_SECONDS,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM host_recording_assignments WHERE session_id = ?", (session_id,)
            ).fetchone()
            return RecordingAssignment._from_row(row)

    def request_stop(self, session_id: str) -> bool:
        """Return whether a recording assignment transitioned to stop."""
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE host_recording_assignments
                SET desired_state = 'stop', revision = revision + 1, updated_at = ?
                WHERE session_id = ? AND desired_state = 'record'
                """,
                (self.clock(), session_id),
            )
            return cursor.rowcount == 1

    def acknowledge(self, host_id: str, recording_id: str, revision: int) -> bool:
        """Return whether the matching host advanced the acknowledged revision."""
        if revision >= 1 << 63 or not recording_id.isascii():
            return False
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE host_recording_assignments SET acked_revision = ?, updated_at = ?
                WHERE host_id = ? AND recording_id = ? AND acked_revision < ? AND ? <= revision
                """,
                (revision, self.clock(), host_id, recording_id, revision, revision),
            )
            return cursor.rowcount == 1

    def mark_ready(
        self,
        host_id: str,
        recording_id: str,
        revision: int,
        host_started_at: float,
        clock_offset: float,
    ) -> bool:
        """Record the first ready signal for the matching active assignment revision."""
        if not 1 <= revision < 1 << 63 or not recording_id.isascii():
            return False
        with self._db() as conn:
            now = self.clock()
            cursor = conn.execute(
                """
                UPDATE host_recording_assignments
                SET ready_at = ?, host_started_at = ?, clock_offset = ?, updated_at = ?
                WHERE host_id = ? AND recording_id = ? AND revision = ?
                    AND desired_state = 'record' AND ready_at IS NULL
                """,
                (now, host_started_at, clock_offset, now, host_id, recording_id, revision),
            )
            return cursor.rowcount == 1

    def wait_ready(
        self, session_id: str, *, timeout_seconds: float = 10.0
    ) -> RecordingAssignment | None:
        """Poll the shared database for an active assignment's first ready signal."""
        deadline = self._monotonic() + timeout_seconds
        while True:
            with self._db() as conn:
                row = conn.execute(
                    "SELECT * FROM host_recording_assignments WHERE session_id = ?", (session_id,)
                ).fetchone()
            if row is None or row["desired_state"] == "stop":
                return None
            if row["ready_at"] is not None:
                return RecordingAssignment._from_row(row)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return None
            self._sleep(min(READY_POLL_SECONDS, remaining))

    def reconcile(self, host_id: str, now: float) -> list[RecordingAssignment]:
        """Return active recordings and stops still awaiting acknowledgment for this host."""
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT * FROM host_recording_assignments WHERE host_id = ? AND (
                    (desired_state = 'record' AND ? < deadline)
                    OR (desired_state = 'stop' AND acked_revision < revision)
                ) ORDER BY created_at, recording_id
                """,
                (host_id, now),
            )
            return [RecordingAssignment._from_row(row) for row in rows]
