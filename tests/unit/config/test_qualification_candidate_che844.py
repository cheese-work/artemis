"""Structural checks for CHE-844's immutable save/relaunch candidate receipt."""

import hashlib
import json
from pathlib import Path


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
    assert workflow["persisted_markers"] == [
        "qual-<run_id>",
        "USD · <unique_decimal>",
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
    assert "nonzero checker exit" in text
    assert "completed` is not control success" in text
    assert "observed name was compared with" in text
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
        assert lane["negative_control"]["required_checker_exit"] == "nonzero"
        assert lane["negative_control"]["not_satisfied_by"] == "completed orchestration status"
        assert lane["negative_control"]["invalid_when_missing"] == "Invalid control; batch unqualified."
        assert lane["negative_control"]["required_evidence"] == [
            "Recovery snapshot confirmation for the original account and USD balance",
            "recorded post-save process stop and verified process end",
            "relaunch evidence for the Recovery snapshot",
            "observed post-relaunch account name",
            "recorded mismatch between the observed account name and qual-<run_id>-WRONG-SUFFIX",
        ]
        assert "verified release" in lane["cleanup_contract"]
