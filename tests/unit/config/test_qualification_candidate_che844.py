"""Structural checks for CHE-844's immutable save/relaunch candidate receipt."""

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

    lanes = {lane["issue"]: lane for lane in receipt["lanes"]}
    assert set(lanes) == {"CHE-540", "CHE-541"}
    assert lanes["CHE-540"]["transport"] == "emulator"
    assert lanes["CHE-541"]["transport"] == "usb_or_wireless_adb"


def test_each_lane_retains_the_execution_controls_and_return_contract():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    for lane in receipt["lanes"]:
        assert lane["evidence_destination"]
        assert "secure-storage" in lane["fixture_reset"]
        assert len(lane["assertions"]) == 3
        assert "WRONG-SUFFIX" in lane["negative_control"]
        assert "verified release" in lane["cleanup_contract"]
