"""Convert the intended negative-control Checker verdict into a process exit."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import UUID

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")
LEDGER_FILENAME = "check_ledger.jsonl"
# Pocket Actual restores a saved picture to Home, which renders the same Recovery
# snapshot composable under its own route tag (CHE-541 batch-4 n01).
SNAPSHOT_ROUTE_TAGS = ("recovery-snapshot", "home")


def _invalid(reason: str) -> int:
    print(json.dumps({"verdict": "invalid_control", "reason": reason}))
    return 2


def _read_records(path: Path) -> list[dict[str, object]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return records if all(isinstance(record, dict) for record in records) else None


def _has_exact_value(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, dict):
        return any(_has_exact_value(child, expected) for child in value.values())
    if isinstance(value, list):
        return any(_has_exact_value(child, expected) for child in value)
    return False


def _has_route_tag(tree: object, tags: tuple[str, ...]) -> bool:
    # The route tag is the screen root's resource-id (testTagsAsResourceId); a text or
    # content-desc "home" (bottom-nav label) is not route identity.
    if isinstance(tree, dict):
        return tree.get("resource_id") in tags or any(
            _has_route_tag(child, tags) for child in tree.values()
        )
    if isinstance(tree, list):
        return any(_has_route_tag(child, tags) for child in tree)
    return False


def _finite_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _step_capture(
    connection: sqlite3.Connection,
    session_id: UUID,
    step_id: UUID,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT s.step_number, s.timestamp, s.pre_image_name, s.action_taken, i.ui_tree
        FROM steps AS s
        JOIN images AS i ON i.image_name = s.pre_image_name
        WHERE s.session_id = ? AND s.step_id = ?
        """,
        (str(session_id), str(step_id)),
    ).fetchone()


def _native_control_proof(
    db_path: Path,
    session_id: UUID,
    saved_step_id: UUID,
    package_name: str,
    observed_account: str,
    expected_balance: str,
    attempt_id: str,
    checker_trace_id: str,
    final_capture_step_id: UUID,
) -> str | None:
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            saved = _step_capture(connection, session_id, saved_step_id)
            capture = _step_capture(connection, session_id, final_capture_step_id)
            checker = connection.execute(
                """
                SELECT 1 FROM traces
                WHERE trace_id = ? AND session_id = ? AND type = 'agent' AND name = 'checker'
                """,
                (checker_trace_id, str(session_id)),
            ).fetchone()
            if saved is None or capture is None or checker is None:
                return None
            saved_timestamp = _finite_timestamp(saved["timestamp"])
            capture_timestamp = _finite_timestamp(capture["timestamp"])
            if saved_timestamp is None or capture_timestamp is None:
                return None
            saved_tree = json.loads(saved["ui_tree"])
            capture_tree = json.loads(capture["ui_tree"])
            if not (
                _has_route_tag(saved_tree, ("recovery-snapshot",))
                and _has_exact_value(saved_tree, observed_account)
                and _has_exact_value(saved_tree, expected_balance)
                and _has_route_tag(capture_tree, SNAPSHOT_ROUTE_TAGS)
                and _has_exact_value(capture_tree, observed_account)
                and _has_exact_value(capture_tree, expected_balance)
            ):
                return None
            capture_action = json.loads(capture["action_taken"])
            if capture_action != {
                "action": "checker_final_capture",
                "attempt_id": attempt_id,
                "checker_trace_id": checker_trace_id,
            }:
                return None
            actions = connection.execute(
                """
                SELECT t.timestamp, t.payload
                FROM traces AS t
                JOIN steps AS s ON s.step_id = t.step_id
                WHERE t.session_id = ? AND s.session_id = ?
                  AND t.type = 'action' AND t.status = 'success'
                """,
                (str(session_id), str(session_id)),
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None

    stop_events: list[float] = []
    launch_events: list[float] = []
    for action in actions:
        try:
            item = json.loads(action["payload"]).get("action")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict) or item.get("app_name") != package_name:
            continue
        timestamp = _finite_timestamp(action["timestamp"])
        if timestamp is None:
            return None
        if item.get("action") == "stop_app":
            stop_events.append(timestamp)
        elif item.get("action") == "launch_app":
            launch_events.append(timestamp)
    if not any(
        saved_timestamp < stop < launch < capture_timestamp
        for stop in stop_events
        for launch in launch_events
    ):
        return None
    return capture["pre_image_name"]


def _latest_final_record(
    records: list[dict[str, object]], expected_account: str
) -> tuple[str, dict[str, object]] | None:
    final_attempts: dict[int, str] = {}
    for record in records:
        attempt_id = record.get("attempt_id")
        if not isinstance(attempt_id, str):
            return None
        if attempt_id.startswith("final#"):
            match = FINAL_ATTEMPT.fullmatch(attempt_id)
            if match is None:
                return None
            final_attempts[int(match.group(1))] = attempt_id
    if not final_attempts:
        return None

    latest_attempt = final_attempts[max(final_attempts)]
    # The Checker words its own items, so match the unique wrong token as a whole
    # word rather than a fixed sentence (CHE-541 n01). Every final assert naming
    # it must have failed; one that passed means the wrong name was "seen".
    token = re.compile(rf"(?<![\w-]){re.escape(expected_account)}(?![\w-])")
    matches = [
        record
        for record in records
        if record["attempt_id"] == latest_attempt
        and record.get("kind") == "assert"
        and isinstance(record.get("item_text"), str)
        and token.search(record["item_text"])
    ]
    if not matches or any(record.get("status") != "failed" for record in matches):
        return None
    # main() trusts one record's capture/trace refs, so all matches must agree on them.
    if len({(r.get("final_capture_step_id"), r.get("trace_id")) for r in matches}) != 1:
        return None
    return latest_attempt, matches[0]


def _final_capture_step_id(record: dict[str, object]) -> UUID | None:
    value = record.get("final_capture_step_id")
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _ledger_path(traces_dir: Path, session_id: UUID) -> Path:
    return traces_dir / str(session_id) / LEDGER_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--data-engine-db", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--saved-step-id", type=UUID, required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--observed-account", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--expected-balance", required=True)
    args = parser.parse_args()

    original_account = args.expected_account.removesuffix("-WRONG-SUFFIX")
    if original_account == args.expected_account or original_account != args.observed_account:
        return _invalid("account_contract")

    ledger = _ledger_path(args.traces_dir, args.session_id)
    records = _read_records(ledger)
    if records is None:
        return _invalid("ledger")
    final = _latest_final_record(records, args.expected_account)
    if final is None:
        return _invalid("checker")
    attempt_id, record = final
    final_capture_step_id = _final_capture_step_id(record)
    checker_trace_id = record.get("trace_id")
    if final_capture_step_id is None or not isinstance(checker_trace_id, str):
        return _invalid("checker")
    image_name = _native_control_proof(
        args.data_engine_db,
        args.session_id,
        args.saved_step_id,
        args.package_name,
        args.observed_account,
        args.expected_balance,
        attempt_id,
        checker_trace_id,
        final_capture_step_id,
    )
    if image_name is None:
        return _invalid("native_capture")

    print(
        json.dumps(
            {
                "verdict": "fail_assertion",
                "observed_account": args.observed_account,
                "expected_account": args.expected_account,
                "ledger_path": str(ledger),
                "final_attempt_id": attempt_id,
                "saved_step_id": str(args.saved_step_id),
                "final_capture_step_id": str(final_capture_step_id),
                "final_capture_image_name": image_name,
                "checker_record": record,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
