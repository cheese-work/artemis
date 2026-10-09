"""Per-module schema revisions for the board tables (docs/board-operations.md, "Migration contract").

Each module owns one row in ``schema_revisions`` and applies its numbered
revisions in order, one transaction per revision, so an interrupted upgrade
resumes at the first revision that did not commit. Revisions only add tables,
indexes and nullable columns; the existing presence checks stay as they are.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_revisions ("
    "module TEXT PRIMARY KEY, revision INTEGER NOT NULL, updated_at REAL NOT NULL)"
)

Revision = Sequence[str]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    applied: list[int]
    backup_path: Path | None


def current_revision(conn: sqlite3.Connection, module: str) -> int:
    row = conn.execute(
        "SELECT revision FROM schema_revisions WHERE module = ?", (module,)
    ).fetchone()
    return row[0] if row else 0


def _online_backup(db_path: Path, module: str) -> Path:
    """Consistent copy including WAL content (a plain file copy would miss it)."""
    dest = db_path.with_name(f"{db_path.name}.pre-{module}.{time.strftime('%Y%m%dT%H%M%S')}")
    source = sqlite3.connect(db_path, timeout=30.0)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


def migrate(db_path: str | Path, module: str, revisions: Sequence[Revision]) -> MigrationReport:
    """Apply ``revisions[n:]`` where ``n`` is the module's recorded revision.

    A database that already holds tables is backed up before the first pending
    revision. A failing revision rolls back alone and re-raises; the ones
    before it stay recorded.
    """
    path = Path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        conn.execute(_DDL)
        done = current_revision(conn, module)
        pending = list(enumerate(revisions[done:], start=done + 1))
        if not pending:
            return MigrationReport([], None)
        has_data = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name != 'schema_revisions' LIMIT 1"
        ).fetchone()
        backup = _online_backup(path, module) if has_data else None
        for number, statements in pending:
            with conn:  # commits the revision, or rolls back all of it
                conn.execute("BEGIN IMMEDIATE")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_revisions (module, revision, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (module) DO UPDATE SET revision = excluded.revision, "
                    "updated_at = excluded.updated_at",
                    (module, number, time.time()),
                )
        return MigrationReport([number for number, _ in pending], backup)
    finally:
        conn.close()
