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

"""Run catalog reads and writes: search, keyset listing, single-run lookup, tombstones."""

import base64
import binascii
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

from artemis.config import TRACES_PATH
from artemis.data_engine import run_catalog

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

_COLUMNS = (
    "m.session_id, s.initial_goal, s.start_time, s.end_time, s.status, s.interrupt_reason, "
    "m.host_id, m.device_ref, m.requested_by, m.pinned, m.deleted_at, m.deleted_reason"
)
_PREFIX = re.compile(r"[0-9a-fA-F]{8}")
_SETTABLE = {"host_id", "requested_by", "pinned", "device_ref"}
_CAPTURE = {"pending", "recording", "stopped", "partial"}
_TRANSFER = {"waiting_for_computer", "uploading", "uploaded", "failed"}
_MAX_CANDIDATES = 20
_MAX_CURSOR_LEN = 512  # a safe-id (128) + a float, base64-encoded, fits with room


class CatalogNotReady(Exception):
    """The catalog tables are missing (migration did not run or failed)."""


class InvalidCursor(ValueError):
    pass


@dataclass(slots=True)
class RunPage:
    runs: list[dict[str, Any]]
    next_cursor: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunLookup:
    """Outcome of resolving one id: exactly one of run / candidates / removed / neither."""

    run: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    removed: dict[str, Any] | None = None


def encode_cursor(start_time: float | None, session_id: str) -> str:
    raw = json.dumps([start_time, session_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[float | None, str]:
    # Our own cursors are ~120 chars; the bound keeps hostile input away from the parser.
    if len(cursor) > _MAX_CURSOR_LEN:
        raise InvalidCursor("malformed cursor")
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise InvalidCursor("malformed cursor") from exc
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        raise InvalidCursor("malformed cursor")
    start = value[0]
    if start is None:
        return None, value[1]
    if isinstance(start, bool) or not isinstance(start, int | float):
        raise InvalidCursor("malformed cursor")
    try:
        start = float(start)  # OverflowError for huge ints
    except OverflowError as exc:
        raise InvalidCursor("malformed cursor") from exc
    if not math.isfinite(start):  # json accepts NaN/Infinity
        raise InvalidCursor("malformed cursor")
    return start, value[1]


def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"]
    try:
        device_ref = json.loads(row["device_ref"]) if row["device_ref"] else None
    except ValueError:
        device_ref = None
    return {
        "session_id": row["session_id"],
        "prompt": row["initial_goal"],
        "status": "completed" if status == "success" else status,
        "interrupt_reason": row["interrupt_reason"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "host_id": row["host_id"],
        "device_ref": device_ref,
        "requested_by": row["requested_by"],
        "pinned": bool(row["pinned"]),
        "recordings": [],
    }


class RunCatalogRepository:
    def __init__(self, db_path=None, traces_dir=None):
        self.db_path = db_path
        self.traces_dir = Path(traces_dir or TRACES_PATH)

    # -- reads ----------------------------------------------------------------

    def list_query(
        self,
        *,
        status: str | None = None,
        device: str | None = None,
        host: str | None = None,
        requester: str | None = None,
        owner: str | None = None,
        since: float | None = None,
        until: float | None = None,
        match: str | None = None,
        terms: list[str] | None = None,
        cursor: tuple[float | None, str] | None = None,
        nulls: bool = False,
        limit: int = 50,
    ) -> tuple[str, list[Any]]:
        """SQL for one page, newest first, seeking past ``cursor`` on the start_time index.

        Runs without a start time sort last; ``nulls=True`` selects that lane, which
        ``list_runs`` queries only once the dated runs are exhausted.
        """
        where = ["m.deleted_at IS NULL"]
        params: list[Any] = []
        if status:
            where.append("s.status IN (?, ?)" if status == "completed" else "s.status = ?")
            params += ["completed", "success"] if status == "completed" else [status]
        if device:
            where.append(
                "CASE WHEN json_valid(m.device_ref) "
                "THEN json_extract(m.device_ref, '$.serial') END = ?"
            )
            params.append(device)
        if host:
            where.append("m.host_id IS NULL" if host == "local" else "m.host_id = ?")
            params += [] if host == "local" else [host]
        if requester:
            where.append("m.requested_by = ?")
            params.append(requester)
        if owner:
            where.append("m.requested_by = ?")
            params.append(owner)
        if since is not None:
            where.append("s.start_time >= ?")
            params.append(since)
        if until is not None:
            where.append("s.start_time < ?")
            params.append(until)
        if match:
            where.append("m.rid IN (SELECT rowid FROM runs_fts WHERE runs_fts MATCH ?)")
            params.append(match)
        if terms:
            clause, term_params = run_catalog.substring_clause(terms)
            where.append(clause)
            params += term_params
        if nulls:
            where.append("s.start_time IS NULL")
            if cursor and cursor[0] is None:
                where.append("s.session_id < ?")
                params.append(cursor[1])
        else:
            where.append("s.start_time IS NOT NULL")
            if cursor:
                where.append("(s.start_time, s.session_id) < (?, ?)")
                params += [cursor[0], cursor[1]]
        sql = (
            f"SELECT {_COLUMNS} FROM sessions s JOIN run_meta m ON m.session_id = s.session_id "
            f"WHERE {' AND '.join(where)} ORDER BY s.start_time DESC, s.session_id DESC LIMIT ?"
        )
        return sql, [*params, limit]

    def list_runs(
        self,
        *,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        **filters: Any,
    ) -> RunPage:
        decoded = decode_cursor(cursor) if cursor else None
        with db_session(self.db_path) as conn:
            mode = self._require_ready(conn)
            warnings = ["search_fallback_substring"] if mode == "substring" else []
            match = terms = None
            if q and q.strip():
                if mode == "fts":
                    match = run_catalog.build_match_query(q)
                    if match is None:
                        return RunPage([], None, warnings)
                else:
                    terms = run_catalog.substring_terms(q)
            rows: list[sqlite3.Row] = []
            for nulls in (False, True):
                if len(rows) > limit or (nulls is False and decoded and decoded[0] is None):
                    continue
                sql, params = self.list_query(
                    match=match,
                    terms=terms,
                    cursor=decoded,
                    nulls=nulls,
                    limit=limit + 1 - len(rows),
                    **filters,
                )
                rows += conn.execute(sql, params).fetchall()
            page, more = rows[:limit], len(rows) > limit
            runs = [_run_from_row(row) for row in page]
            self._attach_recordings(conn, runs)
        next_cursor = (
            encode_cursor(page[-1]["start_time"], page[-1]["session_id"]) if more else None
        )
        return RunPage(runs, next_cursor, warnings)

    def get_run(self, session_id: str) -> RunLookup:
        """Resolve a full id, else an 8-character prefix. Tombstones outrank session rows."""
        session_id = run_catalog.validate_session_id(session_id, base_dir=self.traces_dir)
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            where, params = "m.session_id = ?", [session_id]
            if _PREFIX.fullmatch(session_id) and not self._exists(conn, session_id):
                prefix = session_id.lower()
                upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                where, params = "m.session_id >= ? AND m.session_id < ?", [prefix, upper]
            live = self._fetch(conn, where, params, removed=False)
            if not live:
                gone = self._fetch(conn, where, params, removed=True)
                return RunLookup(removed=_removal(gone[0]) if gone else None)
            runs = [_run_from_row(row) for row in live]
            if len(runs) > 1:
                return RunLookup(candidates=runs[:_MAX_CANDIDATES])
            self._attach_recordings(conn, runs)
            return RunLookup(run=runs[0])

    def owners(self, session_ids: list[str]) -> dict[str, str | None]:
        """``requested_by`` per run id; ids without a run record are left out."""
        found: dict[str, str | None] = {}
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            for start in range(0, len(session_ids), 500):
                chunk = session_ids[start : start + 500]
                marks = ", ".join("?" * len(chunk))
                for row in conn.execute(
                    f"SELECT session_id, requested_by FROM run_meta WHERE session_id IN ({marks})",
                    chunk,
                ):
                    found[row["session_id"]] = row["requested_by"]
        return found

    # -- writes (new writes need canonical uuids) -------------------------------

    def set_meta(self, session_id: str, **fields: Any) -> bool:
        run_catalog.validate_session_id(session_id, strict=True)
        unknown = set(fields) - _SETTABLE
        if unknown:
            raise ValueError(f"not settable: {sorted(unknown)}")
        if "pinned" in fields:
            fields["pinned"] = int(bool(fields["pinned"]))
        if isinstance(fields.get("device_ref"), dict):
            fields["device_ref"] = json.dumps(fields["device_ref"])
        if not fields:
            return False
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            cursor = conn.execute(
                f"UPDATE run_meta SET {assignments} WHERE session_id = ?",
                [*fields.values(), session_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def tombstone(self, session_id: str, reason: str) -> bool:
        """Soft-delete a run. False when unknown or already removed (first reason stands)."""
        run_catalog.validate_session_id(session_id)
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            cursor = conn.execute(
                f"UPDATE run_meta SET deleted_at = {run_catalog.NOW_SQL}, deleted_reason = ? "
                "WHERE session_id = ? AND deleted_at IS NULL",
                (reason, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_recording_state(
        self,
        session_id: str,
        recording_id: str,
        *,
        capture: str | None = None,
        transfer: str | None = None,
    ) -> bool:
        run_catalog.validate_session_id(session_id, strict=True)
        run_catalog.validate_session_id(recording_id)
        if capture is not None and capture not in _CAPTURE and not capture.startswith("missing:"):
            raise ValueError(f"unknown recording capture state: {capture}")
        if transfer is not None and transfer not in _TRANSFER:
            raise ValueError(f"unknown recording transfer state: {transfer}")
        with db_session(self.db_path) as conn:
            self._require_ready(conn)
            cursor = conn.execute(
                "INSERT INTO run_recording_state (session_id, recording_id, capture, transfer, "
                f"updated_at) SELECT ?, ?, ?, ?, {run_catalog.NOW_SQL} "
                "WHERE EXISTS (SELECT 1 FROM run_meta WHERE session_id = ?) "
                "ON CONFLICT (session_id, recording_id) DO UPDATE SET "
                "capture = COALESCE(excluded.capture, capture), "
                "transfer = COALESCE(excluded.transfer, transfer), "
                "updated_at = excluded.updated_at",
                (session_id, recording_id, capture, transfer, session_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # -- internals ----------------------------------------------------------------

    @staticmethod
    def _require_ready(conn: sqlite3.Connection) -> str:
        if not run_catalog.catalog_ready(conn):
            raise CatalogNotReady
        return run_catalog.search_mode(conn)

    @staticmethod
    def _exists(conn: sqlite3.Connection, session_id: str) -> bool:
        return (
            conn.execute("SELECT 1 FROM run_meta WHERE session_id = ?", (session_id,)).fetchone()
            is not None
        )

    @staticmethod
    def _fetch(
        conn: sqlite3.Connection, where: str, params: list[Any], *, removed: bool
    ) -> list[sqlite3.Row]:
        """Live or tombstoned rows, filtered in SQL so the cap never hides the wanted kind."""
        state = "IS NOT NULL" if removed else "IS NULL"
        return conn.execute(
            f"SELECT {_COLUMNS} FROM run_meta m LEFT JOIN sessions s "
            f"ON s.session_id = m.session_id WHERE {where} AND m.deleted_at {state} "
            f"ORDER BY s.start_time DESC, m.session_id DESC LIMIT {_MAX_CANDIDATES + 1}",
            params,
        ).fetchall()

    @staticmethod
    def _attach_recordings(conn: sqlite3.Connection, runs: list[dict[str, Any]]) -> None:
        if not runs:
            return
        by_id = {run["session_id"]: run for run in runs}
        marks = ", ".join("?" * len(by_id))
        for row in conn.execute(
            "SELECT session_id, recording_id, capture, transfer FROM run_recording_state "
            f"WHERE session_id IN ({marks}) ORDER BY recording_id",
            list(by_id),
        ):
            by_id[row["session_id"]]["recordings"].append(
                {
                    "recording_id": row["recording_id"],
                    "capture": row["capture"],
                    "transfer": row["transfer"],
                }
            )


def _removal(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": row["session_id"],
        "reason": row["deleted_reason"],
        "deleted_at": row["deleted_at"],
    }


run_catalog_repo = RunCatalogRepository()
