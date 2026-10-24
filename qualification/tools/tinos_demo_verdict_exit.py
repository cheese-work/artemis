"""Validate Tinos' intended negative control and return its distinct exit."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import UUID

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")
FALSE_ASSERTION = (
    "The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED)."
)
TEST_INTERSTITIAL = "ca-app-pub-3940256099942544/1033173712"
REVENUE_LINE = re.compile(
    rf"revenue \d+ micros \S+ unit={re.escape(TEST_INTERSTITIAL)} "
    r"network=\S+ precision=\S+"
)
FILL_FAILURES = ("not loaded; skipping",)


def _result(verdict: str, reason: str, **details: object) -> int:
    print(json.dumps({"verdict": verdict, "reason": reason, **details}))
    return 1 if verdict == "fail_assertion" else 2


def _read_ledger(path: Path) -> list[dict[str, object]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return records if records and all(isinstance(record, dict) for record in records) else None


def _latest_final_attempt(
    records: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]] | None:
    attempts: dict[int, str] = {}
    for record in records:
        attempt_id = record.get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id.startswith("final#"):
            match = FINAL_ATTEMPT.fullmatch(attempt_id)
            if match is None:
                return None
            attempts[int(match.group(1))] = attempt_id
    if not attempts:
        return None
    latest = attempts[max(attempts)]
    final_records = [record for record in records if record.get("attempt_id") == latest]
    return latest, final_records


def _parse_tinos_messages(logcat_path: Path) -> list[str] | None:
    try:
        lines = logcat_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    messages = []
    for line in lines:
        match = re.search(r"(?:^|\s)TinosDemo:\s*(.*)$", line)
        if match:
            messages.append(match.group(1).strip())
    return messages


def _native_capture(
    db_path: Path,
    session_id: UUID,
    attempt_id: str,
    checker_trace_id: UUID,
    capture_step_id: UUID,
) -> str | None:
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            capture = connection.execute(
                """
                SELECT s.timestamp, s.pre_image_name, s.action_taken, i.ui_tree
                FROM steps AS s
                JOIN images AS i ON i.image_name = s.pre_image_name
                WHERE s.session_id = ? AND s.step_id = ?
                """,
                (str(session_id), str(capture_step_id)),
            ).fetchone()
            checker = connection.execute(
                """
                SELECT 1 FROM traces
                WHERE trace_id = ? AND session_id = ?
                  AND type = 'agent' AND name = 'checker'
                """,
                (str(checker_trace_id), str(session_id)),
            ).fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if "connection" in locals():
            connection.close()

    if capture is None or checker is None:
        return None
    timestamp = capture["timestamp"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    if not math.isfinite(timestamp) or not isinstance(capture["pre_image_name"], str):
        return None
    try:
        action = json.loads(capture["action_taken"])
        ui_tree = json.loads(capture["ui_tree"])
    except (TypeError, json.JSONDecodeError):
        return None
    if action != {
        "action": "checker_final_capture",
        "attempt_id": attempt_id,
        "checker_trace_id": str(checker_trace_id),
    } or not isinstance(ui_tree, (dict, list)):
        return None
    return capture["pre_image_name"]


def _valid_checker_records(
    records: list[dict[str, object]], attempt_id: str
) -> tuple[UUID, UUID] | None:
    if not records:
        return None
    trace_ids = {record.get("trace_id") for record in records}
    capture_ids = {record.get("final_capture_step_id") for record in records}
    if len(trace_ids) != 1 or len(capture_ids) != 1:
        return None
    trace_id_value = next(iter(trace_ids))
    capture_id_value = next(iter(capture_ids))
    if not isinstance(trace_id_value, str) or not isinstance(capture_id_value, str):
        return None
    try:
        trace_id = UUID(trace_id_value)
        capture_id = UUID(capture_id_value)
    except ValueError:
        return None

    failed = []
    false_assertions = []
    for record in records:
        if record.get("attempt_id") != attempt_id:
            return None
        if record.get("checkpoint_id") != "final":
            return None
        if record.get("kind") not in {"assert", "verify"}:
            return None
        status = record.get("status")
        if status not in {"passed", "failed"}:
            return None
        if status == "failed":
            failed.append(record)
        if record.get("kind") == "assert" and record.get("item_text") == FALSE_ASSERTION:
            false_assertions.append(record)
    if len(failed) != 1 or len(false_assertions) != 1 or failed[0] is not false_assertions[0]:
        return None
    if failed[0].get("status") != "failed":
        return None
    return trace_id, capture_id


def validate_control(
    traces_dir: Path,
    db_path: Path,
    session_id: UUID,
    logcat_path: Path,
    runner_exit_status: int,
) -> tuple[int, dict[str, object]]:
    if runner_exit_status != 0:
        return 2, {"verdict": "invalid_control", "reason": "runner_process_failure"}

    messages = _parse_tinos_messages(logcat_path)
    if messages is None:
        return 2, {"verdict": "invalid_control", "reason": "logcat"}
    if any(
        message in FILL_FAILURES or (message.startswith("show failed (") and message.endswith(")"))
        for message in messages
    ):
        return 2, {"verdict": "invalid_control", "reason": "fill_failure"}
    allow_count = messages.count("show allowed: shown and dismissed")
    if allow_count == 0:
        return 2, {"verdict": "NOT-RUN", "reason": "allow_event_missing"}
    if allow_count > 1:
        return 2, {"verdict": "invalid_control", "reason": "allow_event_duplicate"}

    revenue_messages = [message for message in messages if message.startswith("revenue ")]
    if not revenue_messages:
        return 2, {"verdict": "invalid_control", "reason": "revenue_event_missing"}
    if any(REVENUE_LINE.fullmatch(message) is None for message in revenue_messages):
        return 2, {"verdict": "invalid_control", "reason": "revenue_format_or_unit"}

    ledger_path = traces_dir / str(session_id) / "check_ledger.jsonl"
    ledger_records = _read_ledger(ledger_path)
    if ledger_records is None:
        return 2, {"verdict": "invalid_control", "reason": "ledger"}
    latest = _latest_final_attempt(ledger_records)
    if latest is None:
        return 2, {"verdict": "invalid_control", "reason": "checker"}
    attempt_id, final_records = latest
    checker_identity = _valid_checker_records(final_records, attempt_id)
    if checker_identity is None:
        return 2, {"verdict": "invalid_control", "reason": "checker"}
    checker_trace_id, capture_step_id = checker_identity
    image_name = _native_capture(
        db_path,
        session_id,
        attempt_id,
        checker_trace_id,
        capture_step_id,
    )
    if image_name is None:
        return 2, {"verdict": "invalid_control", "reason": "native_capture"}

    return 1, {
        "verdict": "fail_assertion",
        "reason": "intended_false_assertion",
        "session_id": str(session_id),
        "final_attempt_id": attempt_id,
        "checker_trace_id": str(checker_trace_id),
        "final_capture_step_id": str(capture_step_id),
        "final_capture_image_name": image_name,
        "allow_line": "show allowed: shown and dismissed",
        "revenue_line_count": len(revenue_messages),
        "runner_exit_status": runner_exit_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--data-engine-db", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--logcat-file", type=Path, required=True)
    parser.add_argument("--runner-exit-status", type=int, required=True)
    args = parser.parse_args()
    status, output = validate_control(
        args.traces_dir,
        args.data_engine_db,
        args.session_id,
        args.logcat_file,
        args.runner_exit_status,
    )
    print(json.dumps(output))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
