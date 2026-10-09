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

"""Run catalog: side tables over the unified sessions database.

``INSERT ... ON CONFLICT`` and ``INSERT OR REPLACE`` rewrite session rows, so
catalog state never lives in a ``sessions`` column. It lives in ``run_meta``
(one row per session, plus a soft-delete tombstone), ``run_recording_state``
(capture/transfer per recording) and ``runs_fts`` (FTS5 over the prompt and
run_meta text). Triggers keep all three in step with ``sessions``.

Without FTS5 the catalog still works: search falls back to substring matching
and the readiness mode says so.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

logger = logging.getLogger(__name__)

NOW_SQL = "(julianday('now') - 2440587.5) * 86400.0"
_TRIGGERS_FTS = (
    "run_catalog_sessions_ai",
    "run_catalog_sessions_au",
    "run_catalog_sessions_ad",
    "run_catalog_meta_ai",
    "run_catalog_meta_au",
    "run_catalog_meta_ad",
)
_TRIGGERS_PLAIN = ("run_catalog_sessions_ai", "run_catalog_sessions_ad")

_TABLES_DDL = (
    """
CREATE TABLE IF NOT EXISTS run_link_shares (
    email TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (email, session_id)
)""",
    """
CREATE TABLE IF NOT EXISTS run_meta (
    rid INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    host_id TEXT,
    device_ref TEXT,
    requested_by TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    deleted_at REAL,
    deleted_reason TEXT
)""",
    """
CREATE TABLE IF NOT EXISTS run_recording_state (
    session_id TEXT NOT NULL,
    recording_id TEXT NOT NULL,
    capture TEXT,
    transfer TEXT,
    updated_at REAL,
    PRIMARY KEY (session_id, recording_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_start_order ON sessions (start_time DESC, session_id DESC)",
)

# Searchable run_meta text, shared by the index and the substring fallback.
META_TEXT = (
    "coalesce(m.host_id, '') || ' ' || "
    "coalesce(CASE WHEN json_valid(m.device_ref) "
    "THEN json_extract(m.device_ref, '$.serial') END, '') || ' ' || "
    "coalesce(m.requested_by, '')"
)


def _device_ref_sql(column: str) -> str:
    serial = f"coalesce(json_extract({column}, '$.device_id'), json_extract({column}, '$.device_serial'))"
    return (
        f"CASE WHEN json_valid({column}) AND {serial} IS NOT NULL "
        f"THEN json_object('host_id', NULL, 'serial', {serial}) END"
    )


def _refresh(session_id: str) -> str:
    """Re-index one session; tombstoned or deleted runs end up out of the index."""
    return (
        f"DELETE FROM runs_fts WHERE rowid IN "
        f"(SELECT rid FROM run_meta WHERE session_id = {session_id}); "
        f"INSERT INTO runs_fts (rowid, prompt, meta) "
        f"SELECT m.rid, coalesce(s.initial_goal, ''), {META_TEXT} "
        f"FROM run_meta m JOIN sessions s ON s.session_id = m.session_id "
        f"WHERE m.session_id = {session_id} AND m.deleted_at IS NULL;"
    )


def _trigger_ddl(fts: bool) -> list[str]:
    # Not INSERT OR IGNORE: an outer INSERT OR REPLACE on sessions overrides the
    # conflict clause of statements inside a trigger and would replace run_meta.
    create_meta = (
        f"INSERT INTO run_meta (session_id, device_ref) "
        f"SELECT new.session_id, {_device_ref_sql('new.device_info')} "
        f"WHERE NOT EXISTS (SELECT 1 FROM run_meta WHERE session_id = new.session_id);"
    )
    tombstone = (
        f"UPDATE run_meta SET deleted_at = coalesce(deleted_at, {NOW_SQL}), "
        f"deleted_reason = coalesce(deleted_reason, 'session_deleted') "
        f"WHERE session_id = old.session_id;"
    )
    ddl = [
        "CREATE TRIGGER IF NOT EXISTS run_catalog_sessions_ai AFTER INSERT ON sessions BEGIN "
        + create_meta
        + (_refresh("new.session_id") if fts else "")
        + " END",
        "CREATE TRIGGER IF NOT EXISTS run_catalog_sessions_ad AFTER DELETE ON sessions BEGIN "
        + (
            "DELETE FROM runs_fts WHERE rowid IN "
            "(SELECT rid FROM run_meta WHERE session_id = old.session_id); "
            if fts
            else ""
        )
        + tombstone
        + " END",
    ]
    if fts:
        ddl += [
            "CREATE TRIGGER IF NOT EXISTS run_catalog_sessions_au "
            "AFTER UPDATE OF initial_goal ON sessions BEGIN " + _refresh("new.session_id") + " END",
            "CREATE TRIGGER IF NOT EXISTS run_catalog_meta_ai AFTER INSERT ON run_meta BEGIN "
            + _refresh("new.session_id")
            + " END",
            "CREATE TRIGGER IF NOT EXISTS run_catalog_meta_au AFTER UPDATE OF "
            "host_id, device_ref, requested_by, deleted_at ON run_meta BEGIN "
            + _refresh("new.session_id")
            + " END",
            "CREATE TRIGGER IF NOT EXISTS run_catalog_meta_ad AFTER DELETE ON run_meta BEGIN "
            "DELETE FROM runs_fts WHERE rowid = old.rid; END",
        ]
    return ddl


def _has(conn: sqlite3.Connection, kind: str, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ).fetchone()
        is not None
    )


def fts5_available(conn: sqlite3.Connection) -> bool:
    return any("ENABLE_FTS5" in row[0] for row in conn.execute("PRAGMA compile_options"))


def catalog_ready(conn: sqlite3.Connection) -> bool:
    return _has(conn, "table", "run_meta")


def search_mode(conn: sqlite3.Connection) -> str:
    return "fts" if _has(conn, "table", "runs_fts") else "substring"


def _complete(conn: sqlite3.Connection, fts: bool) -> bool:
    wanted = _TRIGGERS_FTS if fts else _TRIGGERS_PLAIN
    return (
        _has(conn, "table", "run_meta")
        and _has(conn, "table", "run_link_shares")
        and _has(conn, "index", "idx_sessions_start_order")
        and all(_has(conn, "trigger", name) for name in wanted)
    )


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Idempotent: tables, index, triggers. False when there is no sessions table yet.

    Publication (tables, trigger replacement, first index build) is one write
    transaction: a concurrent writer waits for it, and a failure rolls back
    everything, so a retry starts from the old consistent state.
    """
    if not _has(conn, "table", "sessions"):
        return False
    fts = _has(conn, "table", "runs_fts") or fts5_available(conn)
    if _complete(conn, fts):
        return True
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    published = False
    try:
        # Another process may have finished while this one waited for the lock.
        fts = _has(conn, "table", "runs_fts") or fts5_available(conn)
        if _complete(conn, fts):
            return True
        for statement in _TABLES_DDL:
            conn.execute(statement)
        promoting = fts and not _has(conn, "table", "runs_fts")
        if promoting:
            conn.execute("CREATE VIRTUAL TABLE runs_fts USING fts5(prompt, meta)")
        # Replace, never keep: triggers from a substring-mode install lack the index upkeep.
        for name in dict.fromkeys(_TRIGGERS_FTS + _TRIGGERS_PLAIN):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        for statement in _trigger_ddl(fts):
            conn.execute(statement)
        if promoting:
            rebuild(conn)  # index the runs created while search was substring-only
        conn.commit()
        published = True
    finally:
        if not published:
            conn.rollback()
    return True


def backfill(conn: sqlite3.Connection) -> int:
    """Create run_meta (and, by trigger, index rows) for sessions that have none."""
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO run_meta (session_id, device_ref) "
        f"SELECT s.session_id, {_device_ref_sql('s.device_info')} FROM sessions s "
        f"WHERE NOT EXISTS (SELECT 1 FROM run_meta m WHERE m.session_id = s.session_id)"
    )
    conn.commit()
    return cursor.rowcount


def rebuild(conn: sqlite3.Connection) -> int:
    """Drop and repopulate the search index from the tables. Returns rows indexed."""
    if search_mode(conn) != "fts":
        return 0
    conn.execute("DELETE FROM runs_fts")
    cursor = conn.execute(
        f"INSERT INTO runs_fts (rowid, prompt, meta) "
        f"SELECT m.rid, coalesce(s.initial_goal, ''), {META_TEXT} "
        f"FROM run_meta m JOIN sessions s ON s.session_id = m.session_id "
        f"WHERE m.deleted_at IS NULL"
    )
    conn.commit()
    return cursor.rowcount


@dataclass(frozen=True, slots=True)
class MigrationReport:
    backfilled: int
    backup_path: Path | None
    search_mode: str


def _online_backup(db_path: Path, label: str = "run-catalog") -> Path:
    """Consistent copy including WAL content (a plain file copy would miss it)."""
    dest = db_path.with_name(f"{db_path.name}.pre-{label}.{time.strftime('%Y%m%dT%H%M%S')}")
    source = sqlite3.connect(db_path, timeout=30.0)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest


def migrate(db_path: str | Path) -> MigrationReport:
    """Install the catalog on an existing database: backup first, then schema + backfill."""
    path = Path(db_path)
    conn = sqlite3.connect(path, timeout=30.0)
    try:
        if not _has(conn, "table", "sessions"):
            return MigrationReport(0, None, "substring")
        backup = None
        if not catalog_ready(conn) and conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            backup = _online_backup(path)
            logger.info("Run catalog migration: database backed up to %s", backup)
        ensure_schema(conn)
        missing = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM sessions s WHERE NOT EXISTS "
            "(SELECT 1 FROM run_meta m WHERE m.session_id = s.session_id))"
        ).fetchone()[0]
        count = backfill(conn) if missing else 0
        mode = search_mode(conn)
        if mode == "substring":
            logger.warning("SQLite FTS5 is unavailable: run search falls back to substring match")
        return MigrationReport(count, backup, mode)
    finally:
        conn.close()


# -- search -------------------------------------------------------------------

_TERM = re.compile(r"[^\W_]+")
_MAX_TERMS = 8
_MAX_TERM_LEN = 64


def build_match_query(text: str) -> str | None:
    """Turn user text into an FTS5 MATCH string made only of quoted literals.

    Every term is a double-quoted phrase joined by implicit AND, so FTS
    operators, column filters and quote characters in user input are plain
    text. The last term also matches as a prefix (search-as-you-type).
    None when the text holds no searchable term.
    """
    terms = [t[:_MAX_TERM_LEN] for t in _TERM.findall(text)][:_MAX_TERMS]
    if not terms:
        return None
    quoted = [f'"{t}"' for t in terms]
    quoted[-1] += "*"
    return " ".join(quoted)


def substring_terms(text: str) -> list[str]:
    return [t[:_MAX_TERM_LEN] for t in text.split()][:_MAX_TERMS]


def like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def substring_clause(terms: list[str]) -> tuple[str, list[str]]:
    clause = " AND ".join(
        f"(s.initial_goal LIKE ? ESCAPE '\\' OR ({META_TEXT}) LIKE ? ESCAPE '\\')" for _ in terms
    )
    return clause, [p for t in terms for p in (like_pattern(t),) * 2]


def search_session_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """Live (not tombstoned) session ids matching ``text``, by FTS or substring."""
    if search_mode(conn) == "fts":
        match = build_match_query(text)
        if match is None:
            return []
        rows = conn.execute(
            "SELECT m.session_id FROM runs_fts f JOIN run_meta m ON m.rid = f.rowid "
            "WHERE runs_fts MATCH ? AND m.deleted_at IS NULL ORDER BY m.rid",
            (match,),
        )
        return [row[0] for row in rows]
    terms = substring_terms(text)
    if not terms:
        return []
    clause, params = substring_clause(terms)
    rows = conn.execute(
        "SELECT s.session_id FROM sessions s JOIN run_meta m ON m.session_id = s.session_id "
        f"WHERE m.deleted_at IS NULL AND {clause} ORDER BY m.rid",
        params,
    )
    return [row[0] for row in rows]


# -- ids ------------------------------------------------------------------------

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def validate_session_id(
    session_id: str, *, strict: bool = False, base_dir: str | Path | None = None
) -> str:
    """Return ``session_id`` if safe to use, else raise ValueError.

    ``strict`` (new writes): canonical lowercase hyphenated uuid only.
    Otherwise (reads of legacy ids): a uuid or ``[A-Za-z0-9._-]{1,128}``, never
    a dot-only name; with ``base_dir`` the id's real path must stay inside it.
    """
    if not isinstance(session_id, str):
        raise ValueError("session id must be a string")
    if strict:
        if not _UUID.fullmatch(session_id):
            raise ValueError("session id must be a canonical lowercase uuid")
        return session_id
    if not _SAFE_ID.fullmatch(session_id) or not session_id.strip("."):
        raise ValueError("invalid session id")
    if base_dir is not None:
        base = os.path.realpath(base_dir)
        target = os.path.realpath(os.path.join(base, session_id))
        if target == base or os.path.commonpath([base, target]) != base:
            raise ValueError("session id escapes the traces directory")
    return session_id
