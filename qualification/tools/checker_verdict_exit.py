"""Convert the intended negative-control Checker verdict into a process exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from uuid import UUID

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")
LEDGER_FILENAME = "check_ledger.jsonl"
OBSERVATION_SCHEMA_VERSION = 1


def _invalid(reason: str) -> int:
    print(json.dumps({"verdict": "invalid_control", "reason": reason}))
    return 2


def _read_records(path: Path) -> list[object] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        return [json.loads(line) for line in text.splitlines() if line]
    except json.JSONDecodeError:
        return None


def _read_observation(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _latest_final_record(
    records: list[object], expected_assertion: str
) -> tuple[str, dict[str, object]] | None:
    final_attempts: dict[int, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        attempt_id = str(record.get("attempt_id", ""))
        match = FINAL_ATTEMPT.fullmatch(attempt_id)
        if match:
            final_attempts[int(match.group(1))] = attempt_id
    if not final_attempts:
        return None

    latest_attempt = final_attempts[max(final_attempts)]
    matches = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("attempt_id") == latest_attempt
        and record.get("kind") == "assert"
        and record.get("item_text") == expected_assertion
    ]
    if len(matches) != 1 or matches[0].get("status") != "failed":
        return None
    return latest_attempt, matches[0]


def _valid_observation(
    observation: dict[str, object],
    session_id: UUID,
    observed_account: str,
    expected_account: str,
) -> bool:
    return observation == {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "attempt_kind": "negative_control",
        "session_id": str(session_id),
        "screen_semantics_id": "recovery-snapshot",
        "after_relaunch": True,
        "observed_account": observed_account,
        "expected_account": expected_account,
    }


def _ledger_path(traces_dir: Path, session_id: UUID) -> Path:
    return traces_dir / str(session_id) / LEDGER_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--observed-account", required=True)
    parser.add_argument("--expected-account", required=True)
    args = parser.parse_args()

    expected_suffix = "-WRONG-SUFFIX"
    original_account = args.expected_account.removesuffix(expected_suffix)
    if original_account == args.expected_account or args.observed_account != original_account:
        return _invalid("account_contract")

    observation = _read_observation(args.observation)
    if observation is None or not _valid_observation(
        observation, args.session_id, args.observed_account, args.expected_account
    ):
        return _invalid("observation")

    ledger = _ledger_path(args.traces_dir, args.session_id)
    records = _read_records(ledger)
    if records is None:
        return _invalid("ledger")
    expected_assertion = f"The post-relaunch account name must equal {args.expected_account}."
    final = _latest_final_record(records, expected_assertion)
    if final is None:
        return _invalid("checker")
    attempt_id, record = final

    print(
        json.dumps(
            {
                "verdict": "fail_assertion",
                "observed_account": args.observed_account,
                "expected_account": args.expected_account,
                "ledger_path": str(ledger),
                "final_attempt_id": attempt_id,
                "checker_record": record,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
