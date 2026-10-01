import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/che656_flash_ac4.v1.json"


def test_receipt_pins_the_flash_pilot_inputs_and_match_evidence():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    source = receipt["source_candidate"]
    runner = source["runner_source"]
    testcase = source["testcase_source"]
    config = source["config_source"]

    assert runner["commit_sha"] == "cbf2fea84cbd47ea54053239679ab521b01d4d23"
    assert config["attachment_id"] == "01a0f55e-b007-725c-9253-4a07a907c5c3"
    assert config["digest_sha256"] == (
        "2c06c1661b4f6cb65cadfa3b2141fa3f6ee7e0d5c399f43aee8bc481a7781274"
    )
    testcase_bytes = (REPO_ROOT / testcase["path"]).read_bytes()
    assert hashlib.sha256(testcase_bytes).hexdigest() == testcase["digest_sha256"]

    pilot = receipt["pilot"]
    assert pilot["goal"] == "Open Settings, find Battery and tell me the current level"
    assert pilot["profile"] == "flash"
    assert pilot["model"] == "anthropic:claude-sonnet-5"
    assert pilot["device_serial"] == "emulator-5556"
    assert pilot["attempt_count"] == 1
    assert pilot["retry_count"] == 0

    expected_files = {
        "attempt_reconciliation_verdict.json",
        "attempt_reconciliation_batch_verdict.json",
    }
    expectations = receipt["summary_verdict_expectations"]
    assert {item["path"] for item in expectations} == expected_files
    assert all(item["accepted"] is True for item in expectations)
    assert all(item["reason"] is None for item in expectations)
    assert receipt["node_match_evidence"]["expected_node_verdict"] == "match"

    reviewer_ids = {
        reviewer["name"]: reviewer["agent_id"]
        for reviewer in receipt["review_scope"]["required_reviewers"]
    }
    assert reviewer_ids == {
        "x99-codex-sol": "5cd75ce5-d323-4004-9779-7e2ccf16bde4",
        "x99-gpt-6-astra": "2e486c8c-88fb-4daf-829e-e96be9c645b3",
    }
    assert "2c5c2f6c-7697-4f84-9ce6-3273fa45538c" not in reviewer_ids.values()
