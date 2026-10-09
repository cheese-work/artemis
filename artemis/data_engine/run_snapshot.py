"""Run facts snapshotted at execution: app build, suite version, device model, agent model.

Nullable ``run_meta`` columns owned by the ``run_meta_ext`` module
(docs/board-operations.md, migration contract). A run writes them when it
executes (:func:`record`); a recorded value is never rewritten. Runs from
before the upgrade are backfilled from data that already exists: the run's
``device_info`` and the host device record. Anything else stays NULL, which the
API reports as unknown, never as a guessed value.

Hook for later layers (build picker, suite CHE-1339, model picker CHE-1331): put
the value in the run's ``device_info`` under the column name before the session
is created; :func:`record` and :func:`backfill` read it from there.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
from typing import Any

from adbutils import AdbError

from artemis.data_engine import run_catalog, schema_revisions

logger = logging.getLogger(__name__)

MODULE = "run_meta_ext"
FIELDS = ("app_build", "suite_version", "device_model", "agent_model")
_MAX_LEN = 128

REVISIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE IF NOT EXISTS backfill_progress (module TEXT PRIMARY KEY, cursor TEXT, "
        "done INTEGER NOT NULL DEFAULT 0, total INTEGER)",
        *(f"ALTER TABLE run_meta ADD COLUMN {field} TEXT" for field in FIELDS),
        # The backfill covers the runs that exist now; later runs are written at execution.
        "INSERT OR IGNORE INTO backfill_progress (module, cursor, done, total) "
        f"SELECT '{MODULE}', '0', 0, COUNT(*) FROM run_meta",
    ),
)


def _from_info(field: str) -> str:
    return (
        "(SELECT CASE WHEN json_valid(s.device_info) THEN "
        f"nullif(substr(trim(json_extract(s.device_info, '$.{field}')), 1, {_MAX_LEN}), '') END "
        "FROM sessions s WHERE s.session_id = run_meta.session_id)"
    )


def _fill_sql(conn: sqlite3.Connection, where: str) -> str:
    """Fill unknown snapshot columns from existing data; known values stay."""
    sources = {field: [_from_info(field)] for field in FIELDS}
    if run_catalog._has(conn, "table", "host_devices"):
        sources["device_model"].append(
            "(SELECT nullif(hd.model, '') FROM host_devices hd "
            "WHERE hd.host_id = run_meta.host_id AND hd.serial = "
            "CASE WHEN json_valid(run_meta.device_ref) "
            "THEN json_extract(run_meta.device_ref, '$.serial') END)"
        )
    sets = ", ".join(f"{f} = coalesce({f}, {', '.join(src)})" for f, src in sources.items())
    return f"UPDATE run_meta SET {sets} WHERE {where}"


def record(conn: sqlite3.Connection, session_id: str) -> None:
    """Snapshot the run's facts at execution. Caller commits."""
    conn.execute(_fill_sql(conn, "session_id = ?"), (session_id,))


def _pending(conn: sqlite3.Connection) -> tuple | None:
    """(cursor, done, total) while the backfill has work left, else None."""
    if not run_catalog._has(conn, "table", "backfill_progress"):
        return None
    row = conn.execute(
        "SELECT cursor, done, total FROM backfill_progress WHERE module = ?", (MODULE,)
    ).fetchone()
    return row if row is not None and row[1] < (row[2] or 0) else None


def backfill(conn: sqlite3.Connection, batch: int = 500) -> int:
    """Resume the backfill from its cursor. Returns rows processed by this call.

    Each batch and its cursor commit together, so an interruption loses at most
    the batch in flight and the restart neither skips nor double-counts a row.
    """
    processed = 0
    while _pending(conn):  # unlocked read: a finished backfill never takes the write lock
        conn.execute("BEGIN IMMEDIATE")
        with conn:  # commits, or rolls back on any error
            row = _pending(conn)
            if row is None:
                return processed
            rids = [
                r[0]
                for r in conn.execute(
                    "SELECT rid FROM run_meta WHERE rid > ? ORDER BY rid LIMIT ?",
                    (int(row[0]), batch),
                )
            ]
            if rids:
                conn.execute(_fill_sql(conn, "rid > ? AND rid <= ?"), (int(row[0]), rids[-1]))
                conn.execute(
                    "UPDATE backfill_progress SET cursor = ?, done = done + ? WHERE module = ?",
                    (str(rids[-1]), len(rids), MODULE),
                )
            else:  # runs removed since the count: nothing left to visit
                conn.execute(
                    "UPDATE backfill_progress SET done = total WHERE module = ?", (MODULE,)
                )
        processed += len(rids)
    return processed


@dataclass(frozen=True, slots=True)
class MigrationReport:
    backfilled: int
    backup_path: Path | None


def migrate(db_path: str | Path) -> MigrationReport:
    """Apply pending ``run_meta_ext`` revisions (``schema_revisions`` backs up first), then resume the backfill."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        if not run_catalog.catalog_ready(conn):
            return MigrationReport(0, None)
        report = schema_revisions.apply(db_path, MODULE, REVISIONS)
        return MigrationReport(backfill(conn), report.backup_path)
    finally:
        conn.close()


def execution_fields(adb_client: Any, device_id: str | None, llm_config: Any) -> dict[str, str]:
    """Facts known when a run starts: device model (adb) and planner model. Unknown ones are left out."""
    fields: dict[str, str] = {}
    try:
        model = adb_client.device(device_id).prop.model if adb_client and device_id else None
    except (AdbError, OSError, RuntimeError) as exc:
        logger.debug("Could not read the device model of %s: %s", device_id, exc)
        model = None
    agent_model = getattr(getattr(llm_config, "planner", None), "model", None)
    for field, value in (("device_model", model), ("agent_model", agent_model)):
        if isinstance(value, str) and value.strip():
            fields[field] = value.strip()
    return fields
