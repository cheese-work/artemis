"""Structural checks for CHE-844's immutable save/relaunch candidate receipt."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/pocket_actual_save_relaunch.che844.v1.json"


def test_receipt_maps_both_qualification_lanes_to_the_same_pinned_source():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["source_candidate"]["commit_sha"] == "f60e89216c5a2c4429520e68d20d17c8b07ae6b3"
    assert receipt["source_candidate"]["manifest_version"] == 3
    assert receipt["source_candidate"]["fork_sha"] == "25a2c5b2c839b674994ee4a2a49e79209142bd1d"
    assert receipt["source_candidate"]["apk_digest_sha256"] == (
        "04795d5f3995c8e895f6440a9763c8a003c8b6ff955541e3965f5e71564e2e12"
    )
    workflow = receipt["source_candidate"]["app_workflow"]
    assert workflow["setup_semantics_id"] == "current-picture-setup"
    assert workflow["account_save_semantics_id"] == "save-account-action"
    assert workflow["balance_semantics_id"] == "verified-balance-amount-input"
    assert workflow["snapshot_semantics_id"] == "recovery-snapshot"
    assert workflow["verified_balance_input"] == "+123.45"
    assert workflow["persisted_markers"] == [
        "qual-<run_id>",
        "USD · +123.45",
        "History gap",
        "Recovery adjustment",
    ]

    lanes = {lane["issue"]: lane for lane in receipt["lanes"]}
    assert set(lanes) == {"CHE-540", "CHE-541"}
    assert lanes["CHE-540"]["transport"] == "emulator"
    assert lanes["CHE-541"]["transport"] == "usb_or_wireless_adb"


def test_receipt_pins_the_revised_journey_bytes():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    journey = REPO_ROOT / receipt["testcase"]["journey_path"]

    assert (
        hashlib.sha256(journey.read_bytes()).hexdigest()
        == receipt["testcase"]["journey_digest_sha256"]
    )


def test_journey_requires_process_death_and_negative_control_provenance():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    text = (REPO_ROOT / receipt["testcase"]["journey_path"]).read_text(encoding="utf-8")

    assert "`stop_app` / `am force-stop`" in text
    assert "process ended" in text
    assert "invalid and cannot pass" in text
    assert "generic checker failure" in text
    assert "checker_verdict_exit.py" in text
    assert "exits `1` only when that exact final `assert`\nfailed" in text
    assert "completed` is not control success" in text
    assert 'observed name was\ncompared with "qual-<run_id>-WRONG-SUFFIX"' in text
    assert "invalid control" in text
    assert "unqualifies the entire batch" in text


def test_each_lane_retains_the_execution_controls_and_return_contract():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    for lane in receipt["lanes"]:
        assert lane["evidence_destination"]
        assert "secure-storage" in lane["fixture_reset"]
        assert len(lane["assertions"]) == 4
        assert lane["relaunch_proof"]["pass_prerequisite"] is True
        assert "force-stop" in lane["relaunch_proof"]["after_save_stop"]
        assert "process ended" in lane["relaunch_proof"]["process_ended"]
        assert lane["negative_control"]["expected_verdict"] == "fail_assertion"
        adapter = lane["negative_control"]["verdict_adapter"]
        assert adapter["path"] == "qualification/tools/checker_verdict_exit.py"
        assert (
            adapter["digest_sha256"]
            == hashlib.sha256((REPO_ROOT / adapter["path"]).read_bytes()).hexdigest()
        )
        assert adapter["expected_exit"] == 1
        assert "check_ledger.jsonl" in adapter["contract"]
        assert lane["negative_control"]["not_satisfied_by"] == "completed orchestration status"
        assert (
            lane["negative_control"]["invalid_when_missing"]
            == "Invalid control; batch unqualified."
        )
        assert lane["negative_control"]["required_evidence"] == [
            "Recovery snapshot confirmation for the original account and USD · +123.45 balance",
            "recorded post-save process stop and verified process end",
            "relaunch evidence for the Recovery snapshot",
            "observed post-relaunch account name",
            "recorded mismatch between the observed account name and qual-<run_id>-WRONG-SUFFIX",
            "final Checker check_ledger.jsonl path and adapter JSON output with exit 1",
        ]
        assert "verified release" in lane["cleanup_contract"]


def test_negative_control_verdict_adapter_exits_one_only_for_the_expected_assertion(tmp_path):
    ledger = tmp_path / "check_ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "attempt_id": "final#1",
                "kind": "assert",
                "status": "failed",
                "item_text": ("The post-relaunch account name must equal qual-run-1-WRONG-SUFFIX."),
                "evidence": "Observed qual-run-1 after relaunch.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    result = subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--ledger",
            str(ledger),
            "--observed-account",
            "qual-run-1",
            "--expected-account",
            "qual-run-1-WRONG-SUFFIX",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["verdict"] == "fail_assertion"


def test_negative_control_verdict_adapter_rejects_a_failed_assertion_without_observed_account(
    tmp_path,
):
    ledger = tmp_path / "check_ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "attempt_id": "final#1",
                "kind": "assert",
                "status": "failed",
                "item_text": ("The post-relaunch account name must equal qual-run-1-WRONG-SUFFIX."),
                "evidence": "The account could not be observed after relaunch.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = REPO_ROOT / "qualification/tools/checker_verdict_exit.py"

    result = subprocess.run(
        [
            sys.executable,
            str(adapter),
            "--ledger",
            str(ledger),
            "--observed-account",
            "qual-run-1",
            "--expected-account",
            "qual-run-1-WRONG-SUFFIX",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["verdict"] == "invalid_control"
