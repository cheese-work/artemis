"""Convert the intended negative-control Checker verdict into a process exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sqlite3
from uuid import UUID

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")
LEDGER_FILENAME = "check_ledger.jsonl"


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


def _native_post_relaunch_image(
    db_path: Path,
    session_id: UUID,
    step_id: UUID,
    package_name: str,
    observed_account: str,
) -> str | None:
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        with connection:
            capture = connection.execute(
                """
                SELECT s.step_number, s.pre_image_name, i.ui_tree
                FROM steps AS s
                JOIN images AS i ON i.image_name = s.pre_image_name
                WHERE s.session_id = ? AND s.step_id = ?
                """,
                (str(session_id), str(step_id)),
            ).fetchone()
            if capture is None:
                return None
            ui_tree = json.loads(capture["ui_tree"])
            if not (
                _has_exact_value(ui_tree, "recovery-snapshot")
                and _has_exact_value(ui_tree, observed_account)
            ):
                return None
            actions = connection.execute(
                """
                SELECT s.step_number, t.payload
                FROM traces AS t
                JOIN steps AS s ON s.step_id = t.step_id
                WHERE t.session_id = ? AND s.session_id = ?
                  AND t.type = 'action' AND t.status = 'success'
                  AND s.step_number < ?
                """,
                (str(session_id), str(session_id), capture["step_number"]),
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None

    stop_steps: list[int] = []
    launch_steps: list[int] = []
    for action in actions:
        try:
            item = json.loads(action["payload"]).get("action")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict) or item.get("app_name") != package_name:
            continue
        if item.get("action") == "stop_app":
            stop_steps.append(action["step_number"])
        elif item.get("action") == "launch_app":
            launch_steps.append(action["step_number"])
    if not any(stop < launch for stop in stop_steps for launch in launch_steps):
        return None
    return capture["pre_image_name"]


def _latest_final_record(
    records: list[dict[str, object]], expected_assertion: str
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
    matches = [
        record
        for record in records
        if record["attempt_id"] == latest_attempt
        and record.get("kind") == "assert"
        and record.get("item_text") == expected_assertion
    ]
    if len(matches) != 1 or matches[0].get("status") != "failed":
        return None
    return latest_attempt, matches[0]


def _ledger_path(traces_dir: Path, session_id: UUID) -> Path:
    return traces_dir / str(session_id) / LEDGER_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--data-engine-db", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--post-relaunch-step-id", type=UUID, required=True)
    parser.add_argument("--package-name", required=True)
    parser.add_argument("--observed-account", required=True)
    parser.add_argument("--expected-account", required=True)
    args = parser.parse_args()

    original_account = args.expected_account.removesuffix("-WRONG-SUFFIX")
    if original_account == args.expected_account or original_account != args.observed_account:
        return _invalid("account_contract")

    ledger = _ledger_path(args.traces_dir, args.session_id)
    records = _read_records(ledger)
    if records is None:
        return _invalid("ledger")
    expected_assertion = f"The post-relaunch account name must equal {args.expected_account}."
    final = _latest_final_record(records, expected_assertion)
    if final is None:
        return _invalid("checker")
    attempt_id, record = final
    image_name = _native_post_relaunch_image(
        args.data_engine_db,
        args.session_id,
        args.post_relaunch_step_id,
        args.package_name,
        args.observed_account,
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
                "post_relaunch_step_id": str(args.post_relaunch_step_id),
                "post_relaunch_image_name": image_name,
                "checker_record": record,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
