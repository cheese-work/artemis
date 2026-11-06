"""Validate the latest final Checker assertions for CHE-3386 qualification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")


def _result(verdict: str, **details: object) -> int:
    print(json.dumps({"verdict": verdict, **details}, sort_keys=True))
    return {"pass": 0, "fail_assertion": 1, "invalid_control": 2}[verdict]


def _latest_assertions(path: Path) -> tuple[str, list[dict[str, object]]] | None:
    try:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not records or not all(isinstance(record, dict) for record in records):
        return None

    attempts: dict[int, str] = {}
    for record in records:
        attempt_id = record.get("attempt_id")
        if not isinstance(attempt_id, str):
            return None
        if attempt_id.startswith("final#"):
            match = FINAL_ATTEMPT.fullmatch(attempt_id)
            if match is None:
                return None
            attempts[int(match.group(1))] = attempt_id
    if not attempts:
        return None

    latest_id = attempts[max(attempts)]
    assertions = [
        record
        for record in records
        if record.get("attempt_id") == latest_id and record.get("kind") == "assert"
    ]
    if not assertions or any(
        not isinstance(record.get("item_text"), str)
        or record.get("status") not in {"passed", "failed"}
        for record in assertions
    ):
        return None
    return latest_id, assertions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--expect", choices=("pass", "negative-control"), required=True)
    parser.add_argument("--token")
    args = parser.parse_args()

    if args.expect == "negative-control" and not args.token:
        return _result("invalid_control", reason="token_required")
    if args.expect == "pass" and args.token is not None:
        return _result("invalid_control", reason="unexpected_token")

    final = _latest_assertions(args.ledger)
    if final is None:
        return _result("invalid_control", reason="checker_ledger")
    attempt_id, assertions = final
    if args.expect == "pass":
        verdict = "pass" if all(item["status"] == "passed" for item in assertions) else "fail_assertion"
        return _result(verdict, final_attempt_id=attempt_id, assertion_count=len(assertions))

    token_pattern = re.compile(rf"(?<![\w-]){re.escape(args.token)}(?![\w-])")
    matching = [item for item in assertions if token_pattern.search(item["item_text"])]
    other = [item for item in assertions if item not in matching]
    if (
        not matching
        or any(item["status"] != "failed" for item in matching)
        or not other
        or any(item["status"] != "passed" for item in other)
    ):
        return _result("invalid_control", reason="assertion_contract", final_attempt_id=attempt_id)
    return _result(
        "fail_assertion",
        final_attempt_id=attempt_id,
        failed_assertion_count=len(matching),
        passed_assertion_count=len(other),
    )


if __name__ == "__main__":
    raise SystemExit(main())
