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

"""Retention, pinning and deletion of runs.

Rules that hold for every automatic deletion: a run that is live (anything not
in a terminal state), still transferring or capturing its recording, or pinned
is never touched. Enforcement is off until an admin enables it, and enabling
needs a dry-run report for the same window first. A delete tombstones the run
at once (the catalog then answers "removed") and removes artifacts when no
download lease is active; otherwise the cleanup is deferred, see ``run_leases``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from artemis.data_engine import run_catalog

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

from apps.admin_console.services import run_leases, run_purge, run_settings
from apps.admin_console.services.run_artifacts import RunLibraryError, library_paths

logger = logging.getLogger(__name__)

DAY = 86400.0
DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 1, 3650
SWEEP_INTERVAL_SECONDS = 6 * 3600.0
TERMINAL = frozenset({"completed", "success", "failed", "cancelled", "interrupted"})
_PENDING_TRANSFER = ("waiting_for_computer", "uploading")
_PENDING_CAPTURE = ("pending", "recording")
_DEFAULTS: dict[str, Any] = {
    "retention_enabled": False,
    "retention_days": DEFAULT_DAYS,
    "retention_dry_run_days": None,
    "retention_last_run_at": None,
}
_RUNS_SQL = (
    "SELECT s.session_id, s.initial_goal AS prompt, s.status, s.start_time, s.end_time, "
    "m.pinned, coalesce(s.end_time, s.start_time) AS ended, EXISTS (SELECT 1 FROM "
    "run_recording_state r WHERE r.session_id = s.session_id AND (r.transfer IN (?, ?) "
    "OR r.capture IN (?, ?))) AS pending_upload "
    "FROM run_meta m JOIN sessions s ON s.session_id = m.session_id WHERE m.deleted_at IS NULL"
)
_PROTECTIONS = ("live", "pending_upload", "pinned")
# The same guards as ``_protection``, as SQL, so the delete itself re-checks them in the
# one UPDATE that tombstones the run: a pin, upload or restart that lands between
# selection and delete makes it a no-op.
_TERMINAL_MARKS = ", ".join("?" * len(TERMINAL))
_GUARDS_SQL = (
    f" AND EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = run_meta.session_id "
    f"AND s.status IN ({_TERMINAL_MARKS}))"
    " AND NOT EXISTS (SELECT 1 FROM run_recording_state r WHERE r.session_id = "
    "run_meta.session_id AND (r.transfer IN (?, ?) OR r.capture IN (?, ?)))"
)


def _protection(row) -> str | None:
    """Why a run must not be deleted automatically (first reason wins), else None."""
    if row["status"] not in TERMINAL:
        return "live"
    if row["pending_upload"]:
        return "pending_upload"
    return "pinned" if row["pinned"] else None


def _runs(conn) -> list:
    return conn.execute(_RUNS_SQL, (*_PENDING_TRANSFER, *_PENDING_CAPTURE)).fetchall()


def _protected_counts(rows) -> dict[str, int]:
    counts = dict.fromkeys(_PROTECTIONS, 0)
    for row in rows:
        reason = _protection(row)
        if reason:
            counts[reason] += 1
    return counts


# -- settings ---------------------------------------------------------------------------


def get_settings() -> dict[str, Any]:
    db_path, _ = library_paths()
    stored = run_settings.read(db_path, _DEFAULTS)
    return {
        "enabled": stored["retention_enabled"],
        "days": stored["retention_days"],
        "dry_run_days": stored["retention_dry_run_days"],
        "last_enforced_at": stored["retention_last_run_at"],
        "pending_cleanups": run_leases.pending_count(db_path),
    }


def _check_days(days: int) -> int:
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise RunLibraryError(422, "invalid_days", min=MIN_DAYS, max=MAX_DAYS)
    return days


def update_settings(*, days: int | None = None, enabled: bool | None = None) -> dict[str, Any]:
    db_path, _ = library_paths()
    current = get_settings()
    changes: dict[str, Any] = {}
    if days is not None and _check_days(days) != current["days"]:
        # A different window is a different deletion: it must be reviewed again.
        changes.update(retention_days=days, retention_enabled=False)
        current.update(days=days, enabled=False)
    if enabled:
        if current["dry_run_days"] != current["days"]:
            raise RunLibraryError(409, "dry_run_required", days=current["days"])
        changes["retention_enabled"] = True
    elif enabled is False:
        changes["retention_enabled"] = False
    if changes:
        run_settings.write(db_path, changes)
    return get_settings()


# -- reports ----------------------------------------------------------------------------


def _expired(rows, days: int, now: float) -> list:
    cutoff = now - days * DAY
    return sorted(
        (r for r in rows if r["ended"] is not None and r["ended"] <= cutoff),
        key=lambda r: (r["ended"], r["session_id"]),
    )


def dry_run(days: int | None = None) -> dict[str, Any]:
    """Exactly what enforcement would delete now; deletes nothing and records the review."""
    db_path, _ = library_paths()
    days = _check_days(days) if days is not None else get_settings()["days"]
    now = time.time()
    with db_session(db_path) as conn:
        expired = _expired(_runs(conn), days, now)
    run_settings.write(db_path, {"retention_dry_run_days": days})
    return {
        "dry_run": True,
        "days": days,
        "generated_at": now,
        "would_delete": [
            {
                "session_id": r["session_id"],
                "prompt": (r["prompt"] or "")[:200],
                "status": r["status"],
                "ended_at": r["ended"],
                "expires_at": r["ended"] + days * DAY,
            }
            for r in expired
            if _protection(r) is None
        ],
        "protected": _protected_counts(expired),
    }


# -- deleting -----------------------------------------------------------------------------


def _lookup(session_id: str) -> Any:
    """The live run with exactly this id; 400, 404 or 410 otherwise."""
    db_path, traces = library_paths()
    try:
        run_catalog.validate_session_id(session_id, base_dir=traces)
    except ValueError as exc:
        raise RunLibraryError(400, "invalid_session_id") from exc
    with db_session(db_path) as conn:
        row = conn.execute(
            _RUNS_SQL + " AND m.session_id = ?", (*_PENDING_TRANSFER, *_PENDING_CAPTURE, session_id)
        ).fetchone()
        if row is not None:
            return row
        gone = conn.execute(
            "SELECT deleted_at, deleted_reason FROM run_meta WHERE session_id = ?", (session_id,)
        ).fetchone()
    if gone is not None and gone["deleted_at"] is not None:
        raise RunLibraryError(
            410, "removed", reason=gone["deleted_reason"], deleted_at=gone["deleted_at"]
        )
    raise RunLibraryError(404, "not_found")


def set_pinned(session_id: str, pinned: bool) -> dict[str, Any]:
    _lookup(session_id)
    db_path, _ = library_paths()
    with db_session(db_path) as conn:
        conn.execute(
            "UPDATE run_meta SET pinned = ? WHERE session_id = ? AND deleted_at IS NULL",
            (int(pinned), session_id),
        )
        conn.commit()
    return {"session_id": session_id, "pinned": pinned}


def _delete(session_id: str, reason: str, *, keep_pinned: bool, vacuum: bool = True) -> str | None:
    """Tombstone, then clean up now or once leases end.

    None when a guard (live, pending upload, and for retention a pin) holds at
    the moment of the delete: nothing is tombstoned or removed.
    """
    db_path, traces = library_paths()
    with db_session(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE run_meta SET deleted_at = {run_catalog.NOW_SQL}, deleted_reason = ? "
            "WHERE session_id = ? AND deleted_at IS NULL"
            + _GUARDS_SQL
            + (" AND pinned = 0" if keep_pinned else ""),
            (
                reason,
                session_id,
                *sorted(TERMINAL),
                *_PENDING_TRANSFER,
                *_PENDING_CAPTURE,
            ),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    if run_leases.request_cleanup(db_path, session_id):
        return "deferred"
    _purge(session_id, vacuum=vacuum)
    return "done"


def _purge(session_id: str, *, vacuum: bool = True) -> None:
    db_path, traces = library_paths()
    run_purge.purge_run(db_path, traces, session_id, vacuum=vacuum)
    run_leases.clear_pending(db_path, session_id)


def delete_run(session_id: str, reason: str = "admin_delete") -> dict[str, Any]:
    """Admin delete of one run (pinned included). A live or uploading run is refused."""
    _refuse_if_busy(_lookup(session_id))
    outcome = _delete(session_id, reason, keep_pinned=False)
    if outcome is None:  # a guard or a concurrent delete got there first
        _refuse_if_busy(_lookup(session_id))
        raise RunLibraryError(409, "run_protected")
    return {"session_id": session_id, "cleanup": outcome}


def _refuse_if_busy(row) -> None:
    reason = _protection(row)
    if reason == "live":
        raise RunLibraryError(409, "run_live")
    if reason == "pending_upload":
        raise RunLibraryError(409, "run_pending_upload")


def _delete_all(session_ids: list[str], reason: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"deleted": [], "deferred": []}
    for session_id in session_ids:
        outcome = _delete(session_id, reason, keep_pinned=True, vacuum=False)
        if outcome:
            result["deleted" if outcome == "done" else "deferred"].append(session_id)
    if result["deleted"]:
        run_purge.compact(library_paths()[0])
    return result


def clearable_count() -> int:
    """How many runs "Clear all" would delete right now (what the admin types to confirm)."""
    db_path, _ = library_paths()
    with db_session(db_path) as conn:
        return sum(1 for row in _runs(conn) if _protection(row) is None)


def clear_all(confirm_count: int) -> dict[str, Any]:
    """Delete every unprotected run, provided the caller typed how many that is."""
    db_path, _ = library_paths()
    with db_session(db_path) as conn:
        rows = _runs(conn)
    targets = [r["session_id"] for r in rows if _protection(r) is None]
    if confirm_count != len(targets):
        raise RunLibraryError(409, "count_mismatch", expected_count=len(targets))
    return {**_delete_all(targets, "admin_clear_all"), "skipped": _protected_counts(rows)}


def enforce() -> dict[str, Any]:
    """Delete the runs a dry run would list. Refused while enforcement is off."""
    settings = get_settings()
    if not settings["enabled"]:
        raise RunLibraryError(409, "retention_disabled")
    db_path, _ = library_paths()
    with db_session(db_path) as conn:
        expired = _expired(_runs(conn), settings["days"], time.time())
    ids = [r["session_id"] for r in expired if _protection(r) is None]
    result = _delete_all(ids, "retention")
    run_settings.write(db_path, {"retention_last_run_at": time.time()})
    return result


# -- deferred cleanups -----------------------------------------------------------------------


def finish_cleanups_for(session_id: str) -> None:
    """A lease just ended: finish this run's cleanup if one is waiting and nothing else holds it."""
    if session_id in run_leases.due_cleanups(library_paths()[0]):
        _purge(session_id)


def finish_pending_cleanups() -> int:
    """Complete every cleanup whose leases are gone (startup, or after a crash)."""
    due = run_leases.due_cleanups(library_paths()[0])
    for session_id in due:
        _purge(session_id)
    return len(due)


async def sweep_forever() -> None:
    """Server background loop: finish due cleanups, enforce retention when enabled."""
    while True:
        try:
            await asyncio.to_thread(finish_pending_cleanups)
            if (await asyncio.to_thread(get_settings))["enabled"]:
                await asyncio.to_thread(enforce)
        except (OSError, sqlite3.Error, RunLibraryError):
            logger.exception("Retention sweep failed; will retry")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
