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
_RUN_EVIDENCE = re.compile(r"\b\w+(Error|Exception)\b: |FlashRunner failed:")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS failure_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
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
    UNIQUE (session_id, step_number)
)""",
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
    """Same cause for the same failure on another device, step or run: digits and case drop out."""
    return f"{rule}|{re.sub(r'\d+', 'N', ' '.join(evidence.lower().split()))[:120]}"


def _ensure(conn: sqlite3.Connection) -> None:
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
SELECT s.session_id, s.interrupt_reason, s.device_info, m.requested_by, m.host_id,
       coalesce(s.end_time, s.start_time, ?) AS at, 0 AS step_number, NULL AS result
FROM sessions s JOIN run_meta m ON m.session_id = s.session_id
WHERE s.status IN ('failed', 'interrupted') AND m.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM failure_ledger f WHERE f.session_id = s.session_id AND f.step_number = 0)
"""
_STEPS_SQL = """
SELECT s.session_id, s.interrupt_reason, s.device_info, m.requested_by, m.host_id,
       coalesce(st.timestamp, s.start_time, ?) AS at, st.step_number, st.last_execution_result AS result
FROM steps st JOIN sessions s ON s.session_id = st.session_id
JOIN run_meta m ON m.session_id = s.session_id
WHERE m.deleted_at IS NULL AND json_valid(st.last_execution_result)
  AND json_extract(st.last_execution_result, '$.status') = 'failed'
  AND NOT EXISTS (SELECT 1 FROM failure_ledger f
                  WHERE f.session_id = st.session_id AND f.step_number = st.step_number)
"""


def collect() -> dict[str, int]:
    """Classify failures not yet in the ledger. Backfills history on the first call."""
    now = time.time()
    traces_dir = run_catalog_repo.traces_dir
    with db_session(run_catalog_repo.db_path) as conn:
        _ensure(conn)
        candidates = [*conn.execute(_RUNS_SQL, (now,)), *conn.execute(_STEPS_SQL, (now,))]
        rows = []
        for row in candidates:
            is_run = row["step_number"] == 0
            raw = _run_evidence(row, traces_dir) if is_run else _step_evidence(row["result"])
            evidence = redact_text(raw)[:_EVIDENCE_MAX]
            category, rule = classify(evidence)
            rows.append(
                (
                    row["session_id"],
                    row["step_number"],
                    "run" if is_run else "step",
                    category,
                    rule,
                    _cause(rule, evidence),
                    evidence,
                    _device(row["device_info"]),
                    "host" if row["host_id"] else "browser",
                    row["requested_by"],
                    row["at"],
                    now,
                )
            )
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO failure_ledger (session_id, step_number, scope, category, rule, "
            "cause, evidence, device, source, owner, occurred_at, classified_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return {"scanned": len(candidates), "inserted": conn.total_changes - before}


def view(days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Counts per day and category, the top repeated causes, and the newest rows."""
    since = time.time() - days * DAY
    with db_session(run_catalog_repo.db_path) as conn:
        _ensure(conn)
        counts = conn.execute(
            "SELECT date(occurred_at, 'unixepoch') AS day, category, COUNT(*) AS count "
            f"FROM failure_ledger l WHERE occurred_at >= ? AND {_LIVE} GROUP BY day, category "
            "ORDER BY day DESC, category",
            (since,),
        ).fetchall()
        causes = conn.execute(
            "SELECT cause, category, rule, COUNT(*) AS count, MAX(occurred_at) AS last_seen, "
            "MIN(evidence) AS sample, json_group_array(DISTINCT session_id) AS session_ids "
            f"FROM failure_ledger l WHERE occurred_at >= ? AND {_LIVE} GROUP BY cause "
            "ORDER BY count DESC, last_seen DESC LIMIT ?",
            (since, _CAUSE_LIMIT),
        ).fetchall()
        rows = conn.execute(
            "SELECT session_id, step_number, scope, category, rule, evidence, device, source, "
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
