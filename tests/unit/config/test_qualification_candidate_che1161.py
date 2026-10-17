import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_PATH = REPO_ROOT / "qualification/candidates/che1161_x99_api35.v1.json"
TESTCASE_PATH = REPO_ROOT / "qualification/journeys/che1161_x99_api35.v1.md"
PILOT_RECEIPT_PATH = REPO_ROOT / "qualification/candidates/che656_flash_ac4.v1.json"


def test_candidate_pins_reviewable_x99_qualification_arms_and_blocked_control():
    assert CANDIDATE_PATH.is_file(), "CHE-1161 qualification candidate is missing"
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))

    assert candidate["issue"] == "CHE-1161"
    assert candidate["runner_source"]["commit_sha"] == "cbf2fea84cbd47ea54053239679ab521b01d4d23"
    assert candidate["baseline_pilot"]["published_head"] == (
        "e0258131c9e52553169368b8b1e1b284de74df99"
    )
    assert candidate["baseline_pilot"]["testcase_digest_sha256"] == (
        "b01d8b8f2f4460dc7b8ed1231bf16ed445104673ad33bd2b39042d1a8c419176"
    )

    testcase = candidate["testcase_source"]
    testcase_bytes = (REPO_ROOT / testcase["path"]).read_bytes()
    assert hashlib.sha256(testcase_bytes).hexdigest() == testcase["digest_sha256"]

    arms = {arm["id"]: arm for arm in candidate["qualification_arms"]}
    assert set(arms) == {"clean_pass", "clean_rerun", "negative_control"}
    for arm_id in ("clean_pass", "clean_rerun"):
        assert arms[arm_id]["expected_verdict"] == "pass"
        assert arms[arm_id]["attempt_count"] == 1
        assert arms[arm_id]["retry_count"] == 0

    negative = arms["negative_control"]
    assert negative["expected_verdict"] == "fail_assertion"
    assert negative["expected_process_exit"] == "nonzero"
    assert negative["execution_status"] == "BLOCKED_SOURCE_GAP"
    assert negative["missing_path"] == "qualification/tools/checker_verdict_exit.py"
    assert negative["executable_command"] is None

    review = candidate["review_requirement"]
    assert review["independent_non_author_decisions"] == 2
    assert review["same_revision"] is True
    assert review["selected_by"] == "x99-codex-sol"
    assert candidate["qualification_ready"] is False
    assert candidate["execution_status"] == "NOT-RUN"


def test_packet_restores_config_and_offline_route_preflight_contract():
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    preflight = candidate["execution_preflight"]

    assert preflight["configuration"]["attachment_id"] == (
        "01a0f55e-b007-725c-9253-4a07a907c5c3"
    )
    assert preflight["configuration"]["sha256"] == (
        "2c06c1661b4f6cb65cadfa3b2141fa3f6ee7e0d5c399f43aee8bc481a7781274"
    )
    assert preflight["configuration"]["environment_variable"] == "ARTEMIS_ARTEMIS_JSONC"
    assert preflight["offline_route"]["helper_attachment_id"] == (
        "01a0f55e-b0df-72a5-a0be-36f0e3e3bca8"
    )
    assert preflight["offline_route"]["sha256"] == (
        "3d963ad481f894f9955b10a46f88b5c0d8b265836c2a0758875e1d91f62b922a"
    )
    assert preflight["offline_route"]["expected_verdict"] == "FLASH_ALL_CLAUDE_SONNET_5"
    assert preflight["failure_action"] == "stop_before_boot"
    assert preflight["secret_values_recorded"] is False

    testcase = TESTCASE_PATH.read_text(encoding="utf-8")
    assert "loaded-settings gate" in testcase
    assert "provider-key and fake-mode checks as booleans only" in testcase
    assert "configuration fallback" in testcase
    assert "offline route" in testcase


def test_packet_restores_boot_attempt_and_receipt_timeout_contract():
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    assert candidate["execution_limits"] == {
        "boot_and_unlocked_ui_readiness_timeout_seconds": 300,
        "whole_attempt_timeout_seconds": 900,
        "timeout_outcome": "failed_or_invalid_no_retry",
    }

    testcase = TESTCASE_PATH.read_text(encoding="utf-8")
    assert "within 300 seconds" in testcase
    assert "exact admitted serial" in testcase
    assert "unlocked UI" in testcase
    assert "900-second wall-clock deadline" in testcase
    assert "failed_or_invalid_no_retry" in testcase
    assert "must not boot or inspect a device" in testcase


def test_packet_binds_clean_arms_to_fresh_admission_and_released_claims():
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    admission = candidate["admission"]
    assert admission["serial_source"] == "fresh exclusive Sol admission"
    assert admission["private_adb_endpoint_source"] == "same fresh exclusive Sol admission"
    assert admission["lifetime"] == "one arm through verified cleanup and release"
    assert admission["released_claim_reusable_for_rerun"] is False
    assert candidate["start_state_contract"]["clean_pass"]
    assert candidate["start_state_contract"]["clean_rerun"]
    assert candidate["start_state_contract"]["rerun_requires_new_admission"] is True
    assert candidate["target_class"]["historical_serial_is_reservation"] is False
    assert candidate["target_class"]["private_adb_endpoint_source"] == (
        "fresh exclusive Sol admission"
    )

    testcase = TESTCASE_PATH.read_text(encoding="utf-8")
    assert "private ADB endpoint from that same admission" in testcase
    assert "clean Settings start state" in testcase
    assert "new admission" in testcase


def test_packet_preserves_pilot_oracles_and_unified_runner_revision():
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))
    pilot = json.loads(PILOT_RECEIPT_PATH.read_text(encoding="utf-8"))
    assert candidate["summary_verdict_expectations"] == pilot["summary_verdict_expectations"]
    assert candidate["node_match_evidence"] == pilot["node_match_evidence"]
    assert candidate["failure_classification_producer"] == (
        "Dedicated X99 Luna execution report from native attempt evidence; Sol verifies"
    )
    assert candidate["page_value_contract"]["source"] == (
        "observed same-attempt Settings > Battery page"
    )
    assert candidate["qualification_revision_contract"]["runner_commit"] == (
        candidate["runner_source"]["commit_sha"]
    )
    assert candidate["qualification_revision_contract"]["support_revision_requires_full_set"] is True
    assert candidate["qualification_revision_contract"]["mixed_runner_revisions_qualify"] is False
    assert candidate["qualification_arms"][2]["execution_status"] == "BLOCKED_SOURCE_GAP"
    assert candidate["qualification_arms"][2]["expected_verdict"] == "NOT-RUN"
