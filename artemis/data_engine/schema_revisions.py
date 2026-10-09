"""Per-module schema revisions and backfill progress (docs/board-operations.md, migration contract).

Each board module owns one ``schema_revisions`` row and applies its numbered
revisions in order. A backfill keeps its cursor in ``backfill_progress`` so an
interrupted run resumes. Existing presence checks (``run_catalog.ensure_schema``)
stay as they are.
"""

from __future__ import annotations

from collections.abc import Sequence
import sqlite3
import time

DDL = (
    "CREATE TABLE IF NOT EXISTS schema_revisions (module TEXT PRIMARY KEY, "
    "revision INTEGER NOT NULL, updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS backfill_progress (module TEXT PRIMARY KEY, cursor TEXT, "
    "done INTEGER NOT NULL DEFAULT 0, total INTEGER)",
)


def current(conn: sqlite3.Connection, module: str) -> int:
    """The module's applied revision; 0 before its first one."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_revisions'"
    ).fetchone():
        return 0
    row = conn.execute(
        "SELECT revision FROM schema_revisions WHERE module = ?", (module,)
    ).fetchone()
    return row[0] if row else 0


def apply(conn: sqlite3.Connection, module: str, revisions: Sequence[Sequence[str]]) -> int:
    """Apply the revisions after the recorded one. Returns how many ran.

    Each revision and its revision bump are one write transaction: a concurrent
    process waits, then sees the revision already applied; a failure rolls back
    and the next start retries from the last recorded revision.
    """
    for statement in DDL:
        conn.execute(statement)
    conn.commit()
    applied = 0
    for number, statements in enumerate(revisions, start=1):
        if current(conn, module) >= number:
            continue
        conn.execute("BEGIN IMMEDIATE")
        with conn:  # commits, or rolls back on any error
            if current(conn, module) < number:  # re-check under the write lock
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_revisions (module, revision, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (module) DO UPDATE SET revision = excluded.revision, "
                    "updated_at = excluded.updated_at",
                    (module, number, time.time()),
                )
                applied += 1
    return applied
