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

    identity = receipt["attempt_identity"]
    assert identity["session_id_argument"] == "--session-id"
    assert identity["run_id_argument"] == "--run-id"
    assert identity["fresh_uuidv4_values_required"] is True
    assert identity["values_must_differ"] is True
    assert identity["record_both_in_returned_evidence"] is True
    assert identity["session_id_binds_manifest_and_native_usage"] is True
    assert identity["run_id_scopes_batch_verdict_to_attempt"] is True

    preflight = receipt["execution_preflight"]
    assert preflight["must_pass_before_device_boot_or_provider_activity"] is True
    assert set(preflight["loaded_settings_google_keys_absent"]) == {
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GCP_API_KEY",
    }
    assert preflight["fake_mode_environment_variable"] == "ARTEMIS_FAKE_LLM"
    assert preflight["fake_mode_enabled_value"] == "1"
    assert preflight["offline_route_source_comment_id"] == ("01a0f55e-b366-78a3-bad8-982de49f1eaa")
    assert preflight["offline_route_script_attachment_id"] == (
        "01a0f55e-b0df-72a5-a0be-36f0e3e3bca8"
    )
    assert preflight["offline_route_script_digest_sha256"] == (
        "3d963ad481f894f9955b10a46f88b5c0d8b265836c2a0758875e1d91f62b922a"
    )
    assert preflight["offline_route_verdict"] == "FLASH_ALL_CLAUDE_SONNET_5"
    assert preflight["record_secret_values"] is False
    assert preflight["failure_action"] == "stop_before_boot"

    limits = receipt["execution_limits"]
    assert limits["boot_and_unlocked_ui_readiness_timeout_seconds"] == 300
    assert limits["whole_attempt_timeout_seconds"] == 900
    assert limits["timeout_outcome"] == "failed_or_invalid_no_retry"

    oracle = receipt["battery_oracle"]
    assert oracle["required_page"] == "Settings > Battery"
    assert oracle["required_field"] == "current-charge percentage"
    assert oracle["same_attempt_privacy_safe_page_evidence_required"] is True
    assert oracle["successful_task_result_required"] is True
    assert oracle["reported_value_must_match_page_field"] is True
    assert oracle["status_bar_only_is_sufficient"] is False

    cleanup = receipt["cleanup_contract"]
    assert cleanup["preserve_available_evidence_before_cleanup"] is True
    assert cleanup["stop_only_recorded_run_owned_processes"] is True
    assert cleanup["restore_settings_changed_by_run"] is True
    assert cleanup["verify_release_before_marking_device_available"] is True
    assert cleanup["release_evidence_return_owner"] == "Android Terra"
    assert (
        cleanup["unverified_cleanup_disposition"]
        == "leave_device_unavailable_with_named_recovery_owner"
    )

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
