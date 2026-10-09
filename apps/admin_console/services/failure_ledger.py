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

"""Failure ledger: every failed step and failed or interrupted run, with a cause category.

Categories tell the dev team what to fix: ``smartqa_infra`` and ``smartqa_agent``
are SmartQA's own faults, ``provider`` is the model gateway, ``user_prompt`` is
the requester's goal (shown, never fixed) and ``unknown`` needs a human.
Classification is rule-based: the first rule whose pattern matches the evidence
text wins. The ledger lives in the run catalog database and is keyed by
(session, step), so collecting again never duplicates a row; step 0 is the run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from apps.admin_console.core.redaction import redact_text
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import run_settings

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session

logger = logging.getLogger(__name__)

DAY = 86400.0
COLLECT_INTERVAL_SECONDS = 3600.0
DEFAULT_DAYS = 14
_EVIDENCE_MAX = 300
_STDOUT_TAIL_BYTES = 64 * 1024
_ROW_LIMIT = 500
_CAUSE_LIMIT = 20
_IDS_PER_CAUSE = 10
_STUCK_AFTER = 60 * 60.0
_GOAL_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "or", "the", "to", "with"}

# (rule, category, pattern), first match wins. Patterns are matched case-insensitively.
_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "device_unavailable",
        "smartqa_infra",
        r"phone disconnected|device\b.{0,60}\b(not found|not available|offline)|adb does not list|adb reports state",
    ),
    (
        "run_interrupted",
        "smartqa_infra",
        r"^interrupted: (host_disconnected|bridge_closed|device_offline|server_restarted)$",
    ),
    ("scrcpy_failure", "smartqa_infra", r"scrcpy"),
    ("accessibility_helper", "smartqa_infra", r"accessibility (helper|service)"),
    ("adb_install", "smartqa_infra", r"adb install|INSTALL_FAILED"),
    (
        "device_unresponsive",
        "smartqa_infra",
        r"device (interface|control)\b.{0,40}\b(unresponsive|stopped)",
    ),
    ("empty_target_list", "smartqa_agent", r"invalid target index \d+\. the list is empty"),
    ("tap_failed", "smartqa_agent", r"tap failed at"),
    ("empty_ui_hierarchy", "smartqa_agent", r"hierarchy is empty|empty (ui )?hierarchy"),
    ("llm_timeout", "provider", r"llm call timed out|timeouterror"),
    (
        "llm_unavailable",
        "provider",
        r"llmexhaustederror|connection error|\b(503|403|429)\b|rate.?limit",
    ),
    ("app_not_installed", "user_prompt", r"error finding package for app"),
    # Runner-internal stops say nothing about the goal. Only the model's own report that the
    # goal cannot be reached counts as prompt-side; any other runner failure stays unknown.
    ("model_no_response", "provider", r"the model returned no response"),
    ("max_turns_reached", "unknown", r"max turns reached"),
    (
        "agent_reported_unachievable",
        "user_prompt",
        r"flashrunner failed:.*(is not installed|isn.t installed|not installed on|impossible|cannot be (done|completed)|does not exist|doesn.t exist)",
    ),
)
_COMPILED = tuple((rule, category, re.compile(p, re.IGNORECASE)) for rule, category, p in _RULES)
_ACTION = {"user_prompt": "no action", "provider": "monitor", "unknown": "review"}
# A traceback's last line, or the runner's own verdict, is a run's cause.
_PREFIX = re.compile(r"^\[[^\]]+\]\s*")  # the runner's "[web_<ts>_<id>]" tag
_RUN_EVIDENCE = re.compile(
    r"\b\w+(Error|Exception)\b: |FlashRunner failed:|^[ \t]+at\s+[\w.$]+\(",
    re.MULTILINE,
)
_RAW_ERROR_TEXT = re.compile(r"^[ \t]*(?:[\w.$]*(?:Error|Exception):|at\s+[\w.$]+\()", re.MULTILINE)
_UUID = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)
_HEX_ID = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_IDENTIFIER_WITH_DIGITS = re.compile(r"\b(?=[a-z0-9_-]*\d)[a-z0-9_-]+\b", re.IGNORECASE)

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS failure_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    signal TEXT NOT NULL DEFAULT 'run_step',
    scope TEXT NOT NULL,
    category TEXT NOT NULL,
    rule TEXT NOT NULL,
    cause TEXT NOT NULL,
    evidence TEXT NOT NULL,
    device TEXT,
    source TEXT NOT NULL,
    owner TEXT,
    occurred_at REAL NOT NULL,
    classified_at REAL NOT NULL,
    UNIQUE (session_id, step_number, signal, cause)
)"""
_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_failure_ledger_time ON failure_ledger (occurred_at)",
    "CREATE TABLE IF NOT EXISTS failure_digest_sent (cause TEXT PRIMARY KEY, sent_at REAL NOT NULL)",
)
_DIGEST_LOCK = threading.Lock()  # ponytail: one server process; a claim table if that changes
_SETTINGS = {"failure_digest_last_at": 0.0}


def classify(evidence: str) -> tuple[str, str]:
    """(category, rule) for one piece of evidence text."""
    for rule, category, pattern in _COMPILED:
        if pattern.search(evidence):
            return category, rule
    return "unknown", "no_match"


def _cause(rule: str, evidence: str) -> str:
    """Same cause across runs when evidence contains variable identifiers or numbers."""
    normalized = " ".join(evidence.lower().split())
    normalized = _UUID.sub("<id>", normalized)
    normalized = _HEX_ID.sub("<id>", normalized)
    normalized = _IDENTIFIER_WITH_DIGITS.sub("N", normalized)
    normalized = re.sub(r"\d+", "N", normalized)
    return f"{rule}|{normalized[:120]}"


def _safe_evidence(evidence: str, goal: str | None, category: str | None = None) -> str:
    if category == "user_prompt":
        evidence = "[prompt-side evidence redacted]"
    elif goal:
        words = set(re.findall(r"[\w]+", goal.casefold()))
        content_words = words - _GOAL_STOP_WORDS
        for word in sorted(content_words or words, key=len, reverse=True):
            evidence = re.sub(
                rf"\b{re.escape(word)}\b", "[goal redacted]", evidence, flags=re.IGNORECASE
            )
    return redact_text(evidence)[:_EVIDENCE_MAX]


def _defect_key(category: str, rule: str, cause: str) -> str:
    normalized_cause = _cause(rule, cause.split("|", 1)[-1])
    return hashlib.sha256(f"{category}|{rule}|{normalized_cause}".encode()).hexdigest()[:16]


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_LEDGER)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(failure_ledger)")}
    unique_keys = []
    for index in conn.execute("PRAGMA index_list(failure_ledger)"):
        if index[2]:
            unique_keys.append(
                tuple(row[2] for row in conn.execute(f"PRAGMA index_info('{index[1]}')"))
            )
    if (
        "signal" not in columns
        or ("session_id", "step_number", "signal", "cause") not in unique_keys
    ):
        signal = (
            "CASE WHEN failure_ledger_legacy.step_number = 0 "
            "AND failure_ledger_legacy.signal = 'stuck_run' "
            "AND EXISTS (SELECT 1 FROM sessions s "
            "WHERE s.session_id = failure_ledger_legacy.session_id AND s.status = 'failed') "
            "THEN 'run_step' ELSE COALESCE(failure_ledger_legacy.signal, 'run_step') END"
            if "signal" in columns
            else "'run_step'"
        )
        conn.execute("SAVEPOINT failure_ledger_signal_migration")
        migration_complete = False
        try:
            conn.execute("ALTER TABLE failure_ledger RENAME TO failure_ledger_legacy")
            conn.execute(_CREATE_LEDGER)
            conn.execute(
                "INSERT INTO failure_ledger "
                "(id, session_id, step_number, signal, scope, category, rule, cause, evidence, "
                "device, source, owner, occurred_at, classified_at) "
                "SELECT id, session_id, step_number, "
                f"{signal}, scope, category, rule, cause, evidence, device, source, owner, "
                "occurred_at, classified_at FROM failure_ledger_legacy"
            )
            legacy_rows = conn.execute(
                "SELECT l.id, l.evidence, l.cause, s.initial_goal, l.category "
                "FROM failure_ledger l LEFT JOIN sessions s ON s.session_id = l.session_id"
            ).fetchall()
            for legacy_row in legacy_rows:
                safe_evidence = _safe_evidence(legacy_row[1], legacy_row[3], legacy_row[4])
                safe_cause = _safe_evidence(legacy_row[2], legacy_row[3], legacy_row[4])
                if safe_evidence != legacy_row[1] or safe_cause != legacy_row[2]:
                    conn.execute(
                        "UPDATE failure_ledger SET evidence = ?, cause = ? WHERE id = ?",
                        (safe_evidence, safe_cause, legacy_row[0]),
                    )
            conn.execute("DROP TABLE failure_ledger_legacy")
            conn.execute("RELEASE SAVEPOINT failure_ledger_signal_migration")
            migration_complete = True
        finally:
            if not migration_complete:
                conn.execute("ROLLBACK TO SAVEPOINT failure_ledger_signal_migration")
                conn.execute("RELEASE SAVEPOINT failure_ledger_signal_migration")
    for statement in _DDL:
        conn.execute(statement)


def forget(conn: sqlite3.Connection, session_id: str) -> None:
    """Drop a run's ledger rows (called when the run is purged). The caller commits."""
    _ensure(conn)
    conn.execute("DELETE FROM failure_ledger WHERE session_id = ?", (session_id,))


# A run that is deleted but whose cleanup is still pending is already gone from the catalog.
_LIVE = (
    "EXISTS (SELECT 1 FROM run_meta m WHERE m.session_id = l.session_id AND m.deleted_at IS NULL)"
)


def _run_evidence(row: sqlite3.Row, traces_dir: Path) -> str:
    if row["interrupt_reason"]:
        return f"interrupted: {row['interrupt_reason']}"
    try:
        with (traces_dir / row["session_id"] / "stdout.log").open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _STDOUT_TAIL_BYTES))
            tail = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if _RUN_EVIDENCE.search(line):
            return _PREFIX.sub("", line.strip(" \t⚠ℹ✅❌"))
    return ""


def _step_evidence(result: str | None) -> str:
    try:
        data = json.loads(result or "")
    except ValueError:
        return str(result or "")
    return str(data.get("error") or data.get("result") or "")


def _device(device_info: str | None) -> str | None:
    try:
        return json.loads(device_info or "").get("device_id")
    except (ValueError, AttributeError):
        return None


_RUNS_SQL = """
SELECT s.session_id, s.interrupt_reason, s.device_info, s.initial_goal AS goal, m.requested_by, m.host_id,
       coalesce(s.end_time, s.start_time, ?) AS at, 0 AS step_number, NULL AS result,
       0 AS stuck, CASE WHEN s.status = 'interrupted' THEN 'stuck_run' ELSE 'run_step' END AS signal
FROM sessions s JOIN run_meta m ON m.session_id = s.session_id
WHERE s.status IN ('failed', 'interrupted') AND m.deleted_at IS NULL
"""
_STUCK_SQL = """
SELECT s.session_id, s.interrupt_reason, s.device_info, s.initial_goal AS goal, m.requested_by, m.host_id,
       ? AS at, 0 AS step_number, NULL AS result, 1 AS stuck, 'stuck_run' AS signal
FROM sessions s JOIN run_meta m ON m.session_id = s.session_id
WHERE s.status IN ('queued', 'running') AND m.deleted_at IS NULL
  AND coalesce(s.start_time, ?) <= ?
"""
_STEPS_SQL = """
SELECT s.session_id, s.interrupt_reason, s.device_info, s.initial_goal AS goal, m.requested_by, m.host_id,
       coalesce(st.timestamp, s.start_time, ?) AS at, st.step_number,
       st.last_execution_result AS result, 0 AS stuck, 'run_step' AS signal
FROM steps st JOIN sessions s ON s.session_id = st.session_id
JOIN run_meta m ON m.session_id = s.session_id
WHERE m.deleted_at IS NULL AND json_valid(st.last_execution_result)
  AND json_extract(st.last_execution_result, '$.status') = 'failed'
  AND NOT EXISTS (SELECT 1 FROM failure_ledger f
                  WHERE f.session_id = st.session_id AND f.step_number = st.step_number
                    AND f.signal = 'run_step')
"""
_RECORDINGS_SQL = """
SELECT s.session_id, s.device_info, s.initial_goal AS goal, m.requested_by, m.host_id,
       coalesce(v.end_time, v.start_time, ?) AS at, 0 AS step_number, v.error AS result,
       v.device_id AS recording_device, 0 AS stuck, 'recording' AS signal
FROM video_recordings v JOIN sessions s ON s.session_id = v.session_id
JOIN run_meta m ON m.session_id = s.session_id
WHERE v.status = 'failed' AND m.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM failure_ledger f
                  WHERE f.session_id = s.session_id AND f.signal = 'recording')
"""


def collect() -> dict[str, int]:
    """Classify failures not yet in the ledger. Backfills history on the first call."""
    now = time.time()
    traces_dir = run_catalog_repo.traces_dir
    with db_session(run_catalog_repo.db_path) as conn:
        _ensure(conn)
        candidates = [
            *conn.execute(_RUNS_SQL, (now,)),
            *conn.execute(_STUCK_SQL, (now, now, now - _STUCK_AFTER)),
            *conn.execute(_STEPS_SQL, (now,)),
            *conn.execute(_RECORDINGS_SQL, (now,)),
        ]
        rows = []
        for row in candidates:
            signal = row["signal"]
            is_run = row["step_number"] == 0
            if row["stuck"]:
                raw = "Run exceeded the one-hour activity threshold"
            elif signal == "recording":
                raw = str(row["result"] or "")
            elif is_run:
                raw = _run_evidence(row, traces_dir)
            else:
                raw = _step_evidence(row["result"])
            evidence = _safe_evidence(raw, row["goal"])
            source = "host" if row["host_id"] else "browser"
            recording_device = row["recording_device"] if "recording_device" in row.keys() else None
            device = recording_device or _device(row["device_info"])
            base = (row["session_id"], row["step_number"], signal, "run" if is_run else "step")
            if signal == "recording":
                from artemis.utils.video import classify_recording_failure

                rule = classify_recording_failure(raw)
                evidence = evidence or "Video recording failed"
                category = "smartqa_infra"
            else:
                category, rule = ("smartqa_infra", "run_stuck") if row["stuck"] else classify(raw)
                evidence = _safe_evidence(raw, row["goal"], category)
            rows.append(
                (
                    *base,
                    category,
                    rule,
                    _cause(rule, evidence),
                    evidence,
                    device,
                    source,
                    row["requested_by"],
                    row["at"],
                    now,
                )
            )
            if _RAW_ERROR_TEXT.search(raw or ""):
                raw_category, raw_rule = classify(raw)
                if raw_rule == "no_match":
                    raw_rule = "raw_error_text"
                raw_error = _safe_evidence(raw, row["goal"], raw_category)
                rows.append(
                    (
                        row["session_id"],
                        row["step_number"],
                        "raw_error_text",
                        "run" if is_run else "step",
                        raw_category,
                        raw_rule,
                        _cause(raw_rule, raw_error),
                        raw_error,
                        device,
                        source,
                        row["requested_by"],
                        row["at"],
                        now,
                    )
                )
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO failure_ledger (session_id, step_number, signal, scope, category, "
            "rule, cause, evidence, device, source, owner, occurred_at, classified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return {"scanned": len(candidates), "inserted": conn.total_changes - before}


def view(days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Counts per day and category, the top repeated causes, and the newest rows."""
    now = time.time()
    since = now - days * DAY
    since_24h = now - DAY
    with db_session(run_catalog_repo.db_path) as conn:
        _ensure(conn)
        counts = conn.execute(
            "SELECT date(occurred_at, 'unixepoch') AS day, category, COUNT(*) AS count "
            f"FROM failure_ledger l WHERE occurred_at >= ? AND {_LIVE} GROUP BY day, category "
            "ORDER BY day DESC, category",
            (since,),
        ).fetchall()
        causes = conn.execute(
            "WITH cause_counts AS ("
            "SELECT cause, signal, scope, category, rule, COUNT(*) AS count, "
            "SUM(CASE WHEN occurred_at >= ? THEN 1 ELSE 0 END) AS count_24h, "
            "MIN(occurred_at) AS first_seen, MAX(occurred_at) AS last_seen, "
            "MIN(evidence) AS sample, MIN(evidence) AS evidence_excerpt, MIN(device) AS device, "
            "json_group_array(DISTINCT session_id) AS session_ids, "
            "category NOT IN ('user_prompt', 'provider') AS smartqa_side "
            f"FROM failure_ledger l WHERE occurred_at >= ? AND {_LIVE} "
            "GROUP BY cause, signal, scope, category, rule"
            "), ranked_causes AS ("
            "SELECT *, ROW_NUMBER() OVER (PARTITION BY smartqa_side "
            "ORDER BY count DESC, last_seen DESC, category, rule, cause) AS side_rank "
            "FROM cause_counts) "
            "SELECT cause, signal, scope, category, rule, count, count_24h, first_seen, last_seen, "
            "sample, evidence_excerpt, device, session_ids, smartqa_side "
            "FROM ranked_causes WHERE side_rank <= ? "
            "ORDER BY count DESC, last_seen DESC, category, rule, cause",
            (since_24h, since, _CAUSE_LIMIT),
        ).fetchall()
        rows = conn.execute(
            "SELECT session_id, step_number, signal, scope, category, rule, evidence, device, source, "
            f"owner, occurred_at FROM failure_ledger l WHERE occurred_at >= ? AND {_LIVE} "
            "ORDER BY occurred_at DESC, session_id, step_number LIMIT ?",
            (since, _ROW_LIMIT),
        ).fetchall()
    return {
        "days": days,
        "counts": [dict(row) for row in counts],
        "causes": [
            {
                **dict(row),
                "session_ids": json.loads(row["session_ids"])[:_IDS_PER_CAUSE],
                "run_ids": json.loads(row["session_ids"])[:_IDS_PER_CAUSE],
                "count_window": row["count"],
                "defect_key": _defect_key(row["category"], row["rule"], row["cause"]),
                "smartqa_side": bool(row["smartqa_side"]),
                "action": _action(row["category"]),
            }
            for row in causes
        ],
        "failures": [{**dict(row), "action": _action(row["category"])} for row in rows],
    }


def _action(category: str) -> str:
    return _ACTION.get(category, "fix")


def _send(title: str, message: str) -> bool:
    from mcp_server.notifiers import notify

    return notify("smartqa-failures", message, title=title, event_type="failure_digest")


def digest() -> dict[str, Any]:
    """One summary of SmartQA-side causes not reported before. Marked reported only once sent.

    Serialized: a second caller waits, then finds the causes already reported.
    """
    with _DIGEST_LOCK:
        return _digest()


def _digest() -> dict[str, Any]:
    with db_session(run_catalog_repo.db_path) as conn:
        _ensure(conn)
        causes = [
            dict(row)
            for row in conn.execute(
                "SELECT l.cause, l.category, l.rule, COUNT(*) AS count, MIN(l.evidence) AS sample, "
                "MIN(l.session_id) AS session_id FROM failure_ledger l "
                f"WHERE {_LIVE} AND l.category LIKE 'smartqa\\_%' ESCAPE '\\' "
                "AND NOT EXISTS (SELECT 1 FROM failure_digest_sent d WHERE d.cause = l.cause) "
                "GROUP BY l.cause ORDER BY count DESC"
            )
        ]
    if not causes:
        return {"sent": False, "causes": []}
    lines = [
        f"- {c['category']}/{c['rule']} ×{c['count']}: {c['sample']} (run {c['session_id'][:8]})"
        for c in causes
    ]
    sent = bool(_send(f"SmartQA: {len(causes)} new failure cause(s)", "\n".join(lines)))
    if sent:
        with db_session(run_catalog_repo.db_path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO failure_digest_sent (cause, sent_at) VALUES (?, ?)",
                [(c["cause"], time.time()) for c in causes],
            )
            conn.commit()
    return {
        "sent": sent,
        "causes": [{k: c[k] for k in ("category", "rule", "count", "sample")} for c in causes],
    }


def _digest_due() -> bool:
    db = run_catalog_repo.db_path
    last = run_settings.read(db, _SETTINGS)["failure_digest_last_at"]
    if time.time() - last < DAY:
        return False
    run_settings.write(db, {"failure_digest_last_at": time.time()})
    return True


async def sweep_forever() -> None:
    """Server background loop: collect hourly, send the digest daily. Only cancellation ends it."""
    while True:
        try:
            await asyncio.to_thread(collect)
            if await asyncio.to_thread(_digest_due):
                await asyncio.to_thread(digest)
        except asyncio.CancelledError:
            raise
        except (sqlite3.Error, OSError, ImportError):
            logger.exception("Failure ledger sweep failed; will retry")
        await asyncio.sleep(COLLECT_INTERVAL_SECONDS)
