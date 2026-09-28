"""Convert the intended negative-control Checker verdict into a process exit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _final_failed_assertion(
    records: list[object], observed_account: str, expected_account: str
) -> dict[str, object] | None:
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        if (
            str(record.get("attempt_id", "")).startswith("final#")
            and record.get("kind") == "assert"
            and record.get("status") == "failed"
            and expected_account in str(record.get("item_text", ""))
            and observed_account in str(record.get("evidence", ""))
        ):
            return record
    return None


def _read_records(path: Path) -> list[object]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--observed-account", required=True)
    parser.add_argument("--expected-account", required=True)
    args = parser.parse_args()

    expected_suffix = "-WRONG-SUFFIX"
    original_account = args.expected_account.removesuffix(expected_suffix)
    if (
        original_account == args.expected_account
        or args.observed_account != original_account
        or not args.ledger.is_file()
    ):
        print(json.dumps({"verdict": "invalid_control"}))
        return 2

    try:
        record = _final_failed_assertion(
            _read_records(args.ledger), args.observed_account, args.expected_account
        )
    except (OSError, json.JSONDecodeError):
        print(json.dumps({"verdict": "invalid_control"}))
        return 2

    if record is None:
        print(json.dumps({"verdict": "invalid_control"}))
        return 2

    print(
        json.dumps(
            {
                "verdict": "fail_assertion",
                "observed_account": args.observed_account,
                "expected_account": args.expected_account,
                "checker_record": record,
            }
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
