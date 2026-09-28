"""Convert the intended negative-control Checker verdict into a process exit."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID
import xml.etree.ElementTree as ET

FINAL_ATTEMPT = re.compile(r"final#([1-9]\d*)")
SHA256 = re.compile(r"[0-9a-f]{64}")
LEDGER_FILENAME = "check_ledger.jsonl"
EVIDENCE_MANIFEST_SCHEMA_VERSION = 2


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


def _read_manifest(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


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


def _hierarchy_text(path: Path) -> str | None:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError):
        return None
    return "\n".join(
        value
        for node in root.iter()
        for value in [node.text, *node.attrib.values()]
        if isinstance(value, str)
    )


def _valid_manifest(
    manifest: dict[str, object],
    manifest_path: Path,
    session_id: UUID,
    observed_account: str,
    expected_account: str,
) -> tuple[str, str] | None:
    required = {
        "schema_version",
        "attempt_kind",
        "session_id",
        "final_attempt_id",
        "post_relaunch_step_id",
        "screen_semantics_id",
        "ui_hierarchy",
    }
    hierarchy = manifest.get("ui_hierarchy")
    if (
        set(manifest) != required
        or manifest.get("schema_version") != EVIDENCE_MANIFEST_SCHEMA_VERSION
        or manifest.get("attempt_kind") != "negative_control"
        or manifest.get("session_id") != str(session_id)
        or manifest.get("screen_semantics_id") != "recovery-snapshot"
        or not isinstance(manifest.get("final_attempt_id"), str)
        or not isinstance(manifest.get("post_relaunch_step_id"), str)
        or not manifest["post_relaunch_step_id"]
        or not isinstance(hierarchy, dict)
        or set(hierarchy) != {"path", "sha256"}
    ):
        return None
    relative_path = hierarchy["path"]
    digest = hierarchy["sha256"]
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or Path(relative_path).is_absolute()
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
    ):
        return None
    try:
        root = manifest_path.parent.resolve()
        hierarchy_path = (root / relative_path).resolve(strict=True)
        hierarchy_path.relative_to(root)
        actual_digest = hashlib.sha256(hierarchy_path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return None
    if actual_digest != digest:
        return None
    text = _hierarchy_text(hierarchy_path)
    if text is None or "recovery-snapshot" not in text or observed_account not in text:
        return None
    original_account = expected_account.removesuffix("-WRONG-SUFFIX")
    if original_account == expected_account or original_account != observed_account:
        return None
    return manifest["final_attempt_id"], manifest["post_relaunch_step_id"]


def _valid_checker_evidence(
    record: dict[str, object],
    session_id: UUID,
    step_id: str,
    observed_account: str,
    expected_account: str,
    hierarchy_digest: str,
) -> bool:
    evidence = record.get("evidence")
    return (
        record.get("trace_id") == str(session_id)
        and record.get("anchor_step_id") == step_id
        and isinstance(evidence, str)
        and f"observed_account={observed_account}" in evidence
        and f"expected_account={expected_account}" in evidence
        and f"ui_hierarchy_sha256={hierarchy_digest}" in evidence
    )


def _ledger_path(traces_dir: Path, session_id: UUID) -> Path:
    return traces_dir / str(session_id) / LEDGER_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces-dir", type=Path, required=True)
    parser.add_argument("--session-id", type=UUID, required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--observed-account", required=True)
    parser.add_argument("--expected-account", required=True)
    args = parser.parse_args()

    manifest = _read_manifest(args.evidence_manifest)
    if manifest is None:
        return _invalid("evidence_manifest")
    valid_manifest = _valid_manifest(
        manifest,
        args.evidence_manifest,
        args.session_id,
        args.observed_account,
        args.expected_account,
    )
    if valid_manifest is None:
        return _invalid("evidence_manifest")
    manifest_attempt_id, step_id = valid_manifest
    hierarchy_digest = manifest["ui_hierarchy"]["sha256"]

    ledger = _ledger_path(args.traces_dir, args.session_id)
    records = _read_records(ledger)
    if records is None:
        return _invalid("ledger")
    expected_assertion = f"The post-relaunch account name must equal {args.expected_account}."
    final = _latest_final_record(records, expected_assertion)
    if final is None:
        return _invalid("checker")
    attempt_id, record = final
    if attempt_id != manifest_attempt_id or not _valid_checker_evidence(
        record,
        args.session_id,
        step_id,
        args.observed_account,
        args.expected_account,
        hierarchy_digest,
    ):
        return _invalid("checker")

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
