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

"""Download leases and deferred cleanup for deleted runs.

A download (a bundle build and its streaming) holds a lease on its run.
Deleting a run tombstones it at once, so no new reader gets in, but while a
lease is active the artifacts stay readable: the cleanup is recorded in
``run_pending_cleanup`` and finished when the last lease is released (or has
expired after a crash) by whoever notices first: lease release, the next
delete, retention, or server startup.
"""

from __future__ import annotations

import sqlite3
import time
import uuid

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

# A lease older than this belongs to a download that died with its server.
LEASE_TTL_SECONDS = 3600.0

_DDL = (
    "CREATE TABLE IF NOT EXISTS run_artifact_leases ("
    "lease_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_run_artifact_leases_session ON run_artifact_leases (session_id)",
    "CREATE TABLE IF NOT EXISTS run_pending_cleanup ("
    "session_id TEXT PRIMARY KEY, requested_at REAL NOT NULL)",
)


def _ensure(conn: sqlite3.Connection) -> None:
    for statement in _DDL:
        conn.execute(statement)


def _active(conn: sqlite3.Connection, session_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM run_artifact_leases WHERE session_id = ? AND created_at + ? > ?",
        (session_id, LEASE_TTL_SECONDS, time.time()),
    ).fetchone()[0]


def acquire(db_path, session_id: str) -> str:
    lease_id = uuid.uuid4().hex
    with db_session(db_path) as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO run_artifact_leases (lease_id, session_id, created_at) VALUES (?, ?, ?)",
            (lease_id, session_id, time.time()),
        )
        conn.commit()
    return lease_id


def release(db_path, lease_id: str) -> str | None:
    """Drop a lease; returns its session id when that session now has a cleanup due."""
    with db_session(db_path) as conn:
        _ensure(conn)
        row = conn.execute(
            "SELECT session_id FROM run_artifact_leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()
        conn.execute("DELETE FROM run_artifact_leases WHERE lease_id = ?", (lease_id,))
        conn.commit()
        if row is None:
            return None
        due = conn.execute(
            "SELECT 1 FROM run_pending_cleanup WHERE session_id = ?", (row[0],)
        ).fetchone()
        return row[0] if due and not _active(conn, row[0]) else None


def enqueue_cleanup(conn: sqlite3.Connection, session_id: str) -> bool:
    """Record a pending cleanup on ``conn`` (the caller commits); True when a lease is active.

    The row stays until ``clear_pending``: a cleanup that crashes half-way is
    finished by the next startup instead of leaving orphaned files.
    """
    conn.execute(
        "INSERT OR IGNORE INTO run_pending_cleanup (session_id, requested_at) VALUES (?, ?)",
        (session_id, time.time()),
    )
    return _active(conn, session_id) > 0


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Open a write transaction with the queue table in place (DDL must come first)."""
    _ensure(conn)
    conn.execute("BEGIN IMMEDIATE")


def due_cleanups(db_path) -> list[str]:
    """Pending cleanups whose leases are all released or expired."""
    with db_session(db_path) as conn:
        _ensure(conn)
        rows = conn.execute("SELECT session_id FROM run_pending_cleanup").fetchall()
        return [row[0] for row in rows if not _active(conn, row[0])]


def pending_count(db_path) -> int:
    with db_session(db_path) as conn:
        _ensure(conn)
        return conn.execute("SELECT COUNT(*) FROM run_pending_cleanup").fetchone()[0]


def clear_pending(db_path, session_id: str) -> None:
    with db_session(db_path) as conn:
        _ensure(conn)
        conn.execute("DELETE FROM run_pending_cleanup WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM run_artifact_leases WHERE session_id = ?", (session_id,))
        conn.commit()
