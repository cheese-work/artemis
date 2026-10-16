"""Per-module schema revisions (contract: docs/board-operations.md, "Migration contract").

Each module owns one ``schema_revisions`` row and applies its numbered revisions
in order. Existing presence checks (``run_catalog.ensure_schema``) stay as they
are. A revision is a tuple of statements; never edit a published one, append.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import time

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS schema_revisions (
    module TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    updated_at REAL NOT NULL
)"""


@dataclass(frozen=True, slots=True)
class RevisionReport:
    applied: tuple[int, ...]
    backup_path: Path | None


def _current(conn: sqlite3.Connection, module: str) -> int:
    row = conn.execute(
        "SELECT revision FROM schema_revisions WHERE module = ?", (module,)
    ).fetchone()
    return row[0] if row else 0


def _holds_runs(conn: sqlite3.Connection) -> bool:
    has_sessions = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
    ).fetchone()
    return bool(has_sessions and conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone())


def _online_backup(db_path: Path, module: str, revision: int) -> Path:
    """Consistent copy including WAL content (a plain file copy would miss it)."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    dest = db_path.with_name(f"{db_path.name}.pre-{module}-r{revision}.{stamp}")
    source = sqlite3.connect(db_path, timeout=30.0)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


def apply(db_path: str | Path, module: str, revisions: Sequence[Sequence[str]]) -> RevisionReport:
    """Apply ``module``'s pending revisions, backing up a database that holds runs first.

    A revision recorded higher than this binary knows (a newer binary ran) is left alone.
    """
    path = Path(db_path)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)  # explicit transactions
    try:
        conn.execute(DDL)
        if _current(conn, module) >= len(revisions):
            return RevisionReport((), None)
        backup = None
        if _holds_runs(conn):
            backup = _online_backup(path, module, _current(conn, module) + 1)
            logger.info("%s migration: database backed up to %s", module, backup)
        conn.execute("BEGIN IMMEDIATE")  # serializes concurrent starters; DDL is transactional
        try:
            start = _current(conn, module)
            applied = tuple(range(start + 1, len(revisions) + 1))
            for number in applied:
                for statement in revisions[number - 1]:
                    conn.execute(statement)
            if applied:
                conn.execute(
                    "INSERT INTO schema_revisions (module, revision, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (module) DO UPDATE SET "
                    "revision = excluded.revision, updated_at = excluded.updated_at",
                    (module, applied[-1], time.time()),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return RevisionReport(applied, backup)
    finally:
        conn.close()
