"""Validate Tinos-specific log and Checker evidence without changing the runner."""

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
ALLOW_MESSAGE = "show allowed: shown and dismissed"
DENIAL_MESSAGE = "show denied by Tinos rules (NOT_ALLOWED)"
TEST_INTERSTITIAL = "ca-app-pub-3940256099942544/1033173712"
FEATURE_TAP_LABEL = "Direct Message"
REVENUE_LINE = re.compile(
    rf"revenue \d+ micros \S+ unit={re.escape(TEST_INTERSTITIAL)} "
    r"network=\S+ precision=\S+"
)
FILL_FAILURES = ("not loaded; skipping",)
LOG_LINE = re.compile(
    r"^(?P<timestamp>\d+(?:\.\d+)?)\s+\d+\s+\d+\s+[VDIWEF]\s+TinosDemo:\s*(?P<message>.*)$"
)
CAPTURE_METADATA_KEYS = {
    "serial",
    "start_cursor",
    "device_start_epoch",
    "host_time_before_epoch",
    "host_time_after_epoch",
    "logcat_pid",
    "runner_exit_status",
    "logcat_stopped",
    "logcat_wait_status",
}


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


def _recognized_message(message: str) -> bool:
    return (
        message in {ALLOW_MESSAGE, DENIAL_MESSAGE, *FILL_FAILURES}
        or message.startswith("revenue ")
        or (message.startswith("show failed (") and message.endswith(")"))
    )


def _parse_tinos_events(logcat_path: Path) -> list[tuple[float, str]] | None:
    try:
        lines = logcat_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    events = []
    for line in lines:
        if "TinosDemo:" not in line:
            continue
        message = line.split("TinosDemo:", 1)[1].strip()
        if not _recognized_message(message):
            continue
        match = LOG_LINE.fullmatch(line)
        if match is None:
            return None
        try:
            timestamp = float(match.group("timestamp"))
        except ValueError:
            return None
        if not math.isfinite(timestamp):
            return None
        events.append((timestamp, match.group("message").strip()))
    return events


def _capture_metadata(
    path: Path,
    device_serial: str,
    runner_exit_status: int,
) -> tuple[float, int, float, float] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        metadata: dict[str, str] = {}
        for line in lines:
            key, value = line.split("=", 1)
            if key in metadata:
                return None
            metadata[key] = value
        if set(metadata) != CAPTURE_METADATA_KEYS:
            return None
        cursor = float(metadata["start_cursor"])
        device_epoch = int(metadata["device_start_epoch"])
        host_before = float(metadata["host_time_before_epoch"])
        host_after = float(metadata["host_time_after_epoch"])
        pid = int(metadata["logcat_pid"])
        captured_runner_status = int(metadata["runner_exit_status"])
        int(metadata["logcat_wait_status"])
    except (OSError, UnicodeError, ValueError):
        return None
    if (
        metadata["serial"] != device_serial
        or not math.isfinite(cursor)
        or cursor < 0
        or cursor != device_epoch
        or device_epoch < 0
        or not math.isfinite(host_before)
        or not math.isfinite(host_after)
        or host_before <= 0
        or host_after < host_before
        or pid <= 0
        or captured_runner_status != runner_exit_status
        or metadata["logcat_stopped"] != "true"
    ):
        return None
    offset = (host_before + host_after) / 2 - device_epoch - 0.5
    uncertainty = (host_after - host_before + 1) / 2
    if not math.isfinite(offset) or not math.isfinite(uncertainty) or uncertainty > 1:
        return None
    return cursor, pid, offset, uncertainty


def _session_step_window(db_path: Path, session_id: UUID) -> tuple[float, float] | None:
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        row = connection.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM steps WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if "connection" in locals():
            connection.close()
    if row is None or row[0] is None or row[1] is None:
        return None
    start, end = row
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
        or not math.isfinite(start)
        or not math.isfinite(end)
        or start > end
    ):
        return None
    return float(start), float(end)


def _session_feature_taps(db_path: Path, session_id: UUID) -> list[float] | None:
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        rows = connection.execute(
            "SELECT timestamp, action_taken FROM steps WHERE session_id = ?",
            (str(session_id),),
        ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    finally:
        if "connection" in locals():
            connection.close()

    tap_times = []
    for timestamp, action_text in rows:
        if action_text is None:
            continue
        try:
            action_data = json.loads(action_text)
        except (TypeError, json.JSONDecodeError):
            return None
        actions = action_data if isinstance(action_data, list) else [action_data]
        for action in actions:
            if not isinstance(action, dict):
                continue
            if action.get("action") == "tap" and action.get("target_text") == FEATURE_TAP_LABEL:
                if (
                    isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or not math.isfinite(timestamp)
                ):
                    return None
                tap_times.append(float(timestamp))
    return sorted(tap_times)


def _event_time_error(
    events: list[tuple[float, str]],
    session_window: tuple[float, float],
    cursor: float,
    first_tap_at: float,
    clock_offset: float,
    clock_uncertainty: float,
) -> str | None:
    start, end = session_window
    cursor_latest_host_time = cursor + clock_offset + clock_uncertainty
    if not start <= first_tap_at <= end or cursor_latest_host_time > first_tap_at:
        return "first_tap_outside_session_window"
    for device_timestamp, _ in events:
        host_timestamp = device_timestamp + clock_offset
        earliest_host_time = host_timestamp - clock_uncertainty
        latest_host_time = host_timestamp + clock_uncertainty
        if device_timestamp < cursor:
            return "event_before_logcat_cursor"
        if earliest_host_time < start or latest_host_time > end:
            return "event_outside_session_window"
        if latest_host_time <= first_tap_at:
            return "event_before_first_tap"
        if earliest_host_time <= first_tap_at:
            return "event_time_order_uncertain"
    return None


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


def _checker_identity(
    records: list[dict[str, object]],
    attempt_id: str,
    expected_failure: str | None,
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
    expected_failures = []
    for record in records:
        if record.get("attempt_id") != attempt_id or record.get("checkpoint_id") != "final":
            return None
        if record.get("kind") not in {"assert", "verify"}:
            return None
        status = record.get("status")
        if status not in {"passed", "failed"}:
            return None
        if status == "failed":
            failed.append(record)
        if (
            expected_failure is not None
            and record.get("kind") == "assert"
            and record.get("item_text") == expected_failure
        ):
            expected_failures.append(record)

    if expected_failure is None:
        if failed:
            return None
    elif len(failed) != 1 or len(expected_failures) != 1 or failed[0] is not expected_failures[0]:
        return None
    return trace_id, capture_id


def _checker_evidence(
    traces_dir: Path,
    db_path: Path,
    session_id: UUID,
    expected_failure: str | None,
) -> tuple[tuple[str, UUID, UUID] | None, str | None]:
    records = _read_ledger(traces_dir / str(session_id) / "check_ledger.jsonl")
    if records is None:
        return None, "checker"
    latest = _latest_final_attempt(records)
    if latest is None:
        return None, "checker"
    attempt_id, final_records = latest
    identity = _checker_identity(final_records, attempt_id, expected_failure)
    if identity is None:
        return None, "checker"
    checker_trace_id, capture_step_id = identity
    image_name = _native_capture(
        db_path,
        session_id,
        attempt_id,
        checker_trace_id,
        capture_step_id,
    )
    if image_name is None:
        return None, "native_capture"
    return (image_name, checker_trace_id, capture_step_id), None


def _control_result(reason: str, **details: object) -> tuple[int, dict[str, object]]:
    verdict = "NOT-RUN" if reason == "allow_event_missing" else "invalid_control"
    return 2, {"verdict": verdict, "reason": reason, **details}


def validate_control(
    traces_dir: Path,
    db_path: Path,
    session_id: UUID,
    logcat_path: Path,
    capture_metadata_path: Path,
    device_serial: str,
    runner_exit_status: int,
) -> tuple[int, dict[str, object]]:
    if runner_exit_status != 0:
        return _control_result("runner_process_failure")
    events = _parse_tinos_events(logcat_path)
    session_window = _session_step_window(db_path, session_id)
    capture = _capture_metadata(capture_metadata_path, device_serial, runner_exit_status)
    if events is None:
        return _control_result("logcat")
    if session_window is None:
        return _control_result("session_window")
    if capture is None:
        return _control_result("capture_metadata")
    tap_times = _session_feature_taps(db_path, session_id)
    if tap_times is None:
        return _control_result("feature_taps")
    if len(tap_times) != 1:
        return _control_result("feature_tap_count")
    first_tap_at = tap_times[0]
    cursor, logcat_pid, clock_offset, clock_uncertainty = capture
    time_error = _event_time_error(
        events,
        session_window,
        cursor,
        first_tap_at,
        clock_offset,
        clock_uncertainty,
    )
    if time_error is not None:
        return _control_result(time_error)

    messages = [message for _, message in events]
    if any(
        message in FILL_FAILURES or (message.startswith("show failed (") and message.endswith(")"))
        for message in messages
    ):
        return _control_result("fill_failure")
    if DENIAL_MESSAGE in messages:
        return _control_result("unexpected_denial_event")
    allow_count = messages.count(ALLOW_MESSAGE)
    if allow_count == 0:
        return _control_result("allow_event_missing")
    if allow_count > 1:
        return _control_result("allow_event_duplicate")

    revenue_messages = [message for message in messages if message.startswith("revenue ")]
    if not revenue_messages:
        return _control_result("revenue_event_missing")
    if any(REVENUE_LINE.fullmatch(message) is None for message in revenue_messages):
        return _control_result("revenue_format_or_unit")

    evidence, evidence_error = _checker_evidence(traces_dir, db_path, session_id, FALSE_ASSERTION)
    if evidence is None:
        return _control_result(evidence_error or "checker")
    image_name, checker_trace_id, capture_step_id = evidence
    return 1, {
        "verdict": "fail_assertion",
        "reason": "intended_false_assertion",
        "session_id": str(session_id),
        "checker_trace_id": str(checker_trace_id),
        "final_capture_step_id": str(capture_step_id),
        "final_capture_image_name": image_name,
        "allow_line": ALLOW_MESSAGE,
        "revenue_line_count": len(revenue_messages),
        "runner_exit_status": runner_exit_status,
        "device_serial": device_serial,
        "first_tap_timestamp": first_tap_at,
        "logcat_pid": logcat_pid,
        "logcat_start_cursor": cursor,
        "device_to_host_offset_seconds": clock_offset,
        "clock_uncertainty_seconds": clock_uncertainty,
    }


def _positive_result(
    exit_status: int,
    verdict: str,
    reason: str,
    **details: object,
) -> tuple[int, dict[str, object]]:
    return exit_status, {"verdict": verdict, "reason": reason, **details}


def validate_positive(
    traces_dir: Path,
    db_path: Path,
    session_id: UUID,
    logcat_path: Path,
    capture_metadata_path: Path,
    device_serial: str,
    first_visible_at: float,
    closed_at: float,
    runner_exit_status: int,
) -> tuple[int, dict[str, object]]:
    if runner_exit_status != 0:
        return _positive_result(12, "INVALID-EVIDENCE", "runner_process_failure")
    events = _parse_tinos_events(logcat_path)
    session_window = _session_step_window(db_path, session_id)
    capture = _capture_metadata(capture_metadata_path, device_serial, runner_exit_status)
    if events is None:
        return _positive_result(12, "INVALID-EVIDENCE", "logcat")
    if session_window is None:
        return _positive_result(12, "INVALID-EVIDENCE", "session_window")
    if capture is None:
        return _positive_result(12, "INVALID-EVIDENCE", "capture_metadata")
    tap_times = _session_feature_taps(db_path, session_id)
    if tap_times is None:
        return _positive_result(12, "INVALID-EVIDENCE", "feature_taps")
    if len(tap_times) != 2:
        return _positive_result(12, "INVALID-EVIDENCE", "feature_tap_count")
    first_tap_at, second_tap_at = tap_times
    cursor, logcat_pid, clock_offset, clock_uncertainty = capture
    time_error = _event_time_error(
        events,
        session_window,
        cursor,
        first_tap_at,
        clock_offset,
        clock_uncertainty,
    )
    if time_error is not None:
        return _positive_result(12, "INVALID-EVIDENCE", time_error)
    start, end = session_window
    explicit_times = (first_visible_at, closed_at)
    if any(not math.isfinite(value) or value < start or value > end for value in explicit_times):
        return _positive_result(12, "INVALID-EVIDENCE", "action_time_outside_session_window")
    if not first_tap_at <= first_visible_at < closed_at < second_tap_at:
        return _positive_result(12, "INVALID-EVIDENCE", "action_time_order")
    visible_duration = closed_at - first_visible_at
    if visible_duration > 15:
        return _positive_result(11, "NOT-RUN", "close_deadline_missed")
    if visible_duration <= 0:
        return _positive_result(12, "INVALID-EVIDENCE", "invalid_visible_duration")

    messages = [message for _, message in events]
    if any(
        message in FILL_FAILURES or (message.startswith("show failed (") and message.endswith(")"))
        for message in messages
    ):
        return _positive_result(11, "NOT-RUN", "fill_failure")
    allows = [
        (timestamp + clock_offset, message)
        for timestamp, message in events
        if message == ALLOW_MESSAGE
    ]
    if not allows:
        return _positive_result(11, "NOT-RUN", "allow_event_missing")
    allows.sort(key=lambda event: event[0])
    allow_at = allows[0][0]
    if allow_at - clock_uncertainty <= closed_at:
        return _positive_result(12, "INVALID-EVIDENCE", "allow_before_close")
    if second_tap_at <= allow_at + clock_uncertainty:
        return _positive_result(12, "INVALID-EVIDENCE", "second_tap_before_allow")

    elapsed = second_tap_at - allow_at
    if elapsed > 15 - visible_duration or elapsed + visible_duration + 5 > 20:
        return _positive_result(11, "NOT-RUN", "timing_window_missed")

    denials = [
        (timestamp + clock_offset, message)
        for timestamp, message in events
        if message == DENIAL_MESSAGE
    ]
    revenue_messages = [message for message in messages if message.startswith("revenue ")]
    revenue_observation = (
        "missing"
        if not revenue_messages
        else "malformed"
        if any(REVENUE_LINE.fullmatch(message) is None for message in revenue_messages)
        else "present"
    )
    if len(allows) > 1:
        if not denials and len(allows) == 2:
            second_allow_at = allows[1][0]
            if second_allow_at - clock_uncertainty <= second_tap_at:
                return _positive_result(12, "INVALID-EVIDENCE", "second_allow_before_second_tap")
            return _positive_result(
                10,
                "FAIL",
                "gate_allowed_inside_interval",
                first_tap_timestamp=first_tap_at,
                second_tap_timestamp=second_tap_at,
                allow_timestamp=allow_at,
                second_allow_timestamp=second_allow_at,
                visible_duration_seconds=visible_duration,
                revenue_observation=revenue_observation,
                revenue_line_count=len(revenue_messages),
                device_serial=device_serial,
                logcat_pid=logcat_pid,
            )
        return _positive_result(12, "INVALID-EVIDENCE", "allow_event_duplicate")
    if not revenue_messages:
        return _positive_result(10, "FAIL", "revenue_event_missing")
    if any(REVENUE_LINE.fullmatch(message) is None for message in revenue_messages):
        return _positive_result(10, "FAIL", "revenue_format_or_unit")

    if not denials:
        return _positive_result(10, "FAIL", "denial_event_missing")
    if len(denials) != 1:
        return _positive_result(12, "INVALID-EVIDENCE", "denial_event_duplicate")
    denial_at = denials[0][0]
    if denial_at + clock_uncertainty <= second_tap_at:
        return _positive_result(12, "INVALID-EVIDENCE", "denial_before_second_tap")
    if denial_at - clock_uncertainty <= second_tap_at:
        return _positive_result(12, "INVALID-EVIDENCE", "denial_order_uncertain")

    evidence, evidence_error = _checker_evidence(traces_dir, db_path, session_id, None)
    if evidence is None:
        return _positive_result(12, "INVALID-EVIDENCE", evidence_error or "checker")
    image_name, checker_trace_id, capture_step_id = evidence
    return _positive_result(
        0,
        "PASS",
        "positive_events_and_timing_verified",
        session_id=str(session_id),
        checker_trace_id=str(checker_trace_id),
        final_capture_step_id=str(capture_step_id),
        final_capture_image_name=image_name,
        allow_timestamp=allow_at,
        revenue_line_count=len(revenue_messages),
        denial_timestamp=denial_at,
        first_tap_timestamp=first_tap_at,
        second_tap_timestamp=second_tap_at,
        visible_duration_seconds=visible_duration,
        logcat_start_cursor=cursor,
        device_to_host_offset_seconds=clock_offset,
        clock_uncertainty_seconds=clock_uncertainty,
        logcat_pid=logcat_pid,
        device_serial=device_serial,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("control", "positive"), required=True)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--data-engine-db", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--logcat-file", type=Path, required=True)
    parser.add_argument("--capture-metadata-file", type=Path, required=True)
    parser.add_argument("--device-serial", required=True)
    parser.add_argument("--runner-exit-status", type=int, required=True)
    parser.add_argument("--first-visible-at", type=float)
    parser.add_argument("--closed-at", type=float)
    args = parser.parse_args()

    if args.mode == "positive":
        if None in (args.first_visible_at, args.closed_at):
            parser.error("positive mode requires first-visible and closed timestamps")
        status, output = validate_positive(
            args.traces_dir,
            args.data_engine_db,
            args.session_id,
            args.logcat_file,
            args.capture_metadata_file,
            args.device_serial,
            args.first_visible_at,
            args.closed_at,
            args.runner_exit_status,
        )
    else:
        status, output = validate_control(
            args.traces_dir,
            args.data_engine_db,
            args.session_id,
            args.logcat_file,
            args.capture_metadata_file,
            args.device_serial,
            args.runner_exit_status,
        )
    print(json.dumps(output))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
