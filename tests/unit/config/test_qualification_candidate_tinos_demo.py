"""Structural checks for CHE-2016's Tinos Artemis testcase candidate."""

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/tinos_demo_gated_interstitial.che2016.v1.json"
JOURNEY_PATH = REPO_ROOT / "qualification/journeys/tinos_demo_gated_interstitial.md"
SOURCE_MANIFEST_PATH = REPO_ROOT / "qualification/manifests/pocket_actual_save_relaunch.v1.json"
SOURCE_JOURNEY_PATH = REPO_ROOT / "qualification/journeys/pocket_actual_save_relaunch.md"
RUNNER_SHA = "feefd94c72dbce0fa1afc109ffe3d8254f565d6b"
TESTCASE_SOURCE_SHA = "096411113522abc37964256833fecbbac4778af4"
APP_SHA = "3277551509acfdfa2615491a6b197cec741e4388"
APK_SHA256 = "d5b3e52c40941dd0b9e473d04029d61ca64e9369e721e947eb2eef470791dc08"


def test_receipt_pins_runner_testcase_manifest_and_unchanged_app_build():
    assert RECEIPT_PATH.is_file(), f"missing candidate receipt: {RECEIPT_PATH}"
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    source = receipt["source_candidate"]
    runner = source["runner_source"]
    testcase = source["testcase_source"]
    assert runner["commit_sha"] == RUNNER_SHA
    assert testcase["commit_sha"] == TESTCASE_SOURCE_SHA
    assert testcase["manifest_version"] == 9
    assert testcase["fork_sha"] == RUNNER_SHA

    manifest_bytes = SOURCE_MANIFEST_PATH.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == testcase["manifest_digest_sha256"]
    assert json.loads(manifest_bytes)["manifest_version"] == 9
    assert json.loads(manifest_bytes)["fork_sha"]["value"] == RUNNER_SHA
    source_journey_bytes = SOURCE_JOURNEY_PATH.read_bytes()
    assert hashlib.sha256(source_journey_bytes).hexdigest() == testcase[
        "journey_digest_sha256"
    ]

    app = source["app_input_source"]
    assert app == {
        "repository": "https://github.com/tevo-studio/sa049-dual-app",
        "pull_request": 145,
        "commit_sha": APP_SHA,
    }
    assert source["app_sha"] == APP_SHA
    assert source["apk_digest_sha256"] == APK_SHA256
    assert source["apk_size_bytes"] == 67183120
    assert source["package_name"] == "com.dualix.dualmessenger.webscanner"
    assert receipt["testcase"]["journey_path"] == str(JOURNEY_PATH.relative_to(REPO_ROOT))
    assert receipt["testcase"]["journey_digest_sha256"] == hashlib.sha256(
        JOURNEY_PATH.read_bytes()
    ).hexdigest()


def test_journey_distinguishes_allowed_show_denial_and_fill_failure():
    assert JOURNEY_PATH.is_file(), f"missing candidate journey: {JOURNEY_PATH}"
    journey = " ".join(JOURNEY_PATH.read_text(encoding="utf-8").split())

    for expected in (
        "TinosDemo",
        "show allowed: shown and dismissed",
        "show denied by Tinos rules (NOT_ALLOWED)",
        "not loaded; skipping",
        "show failed (<code>)",
        "revenue ",
        "main",
        "20 seconds",
        "3 shows per hour",
        "non-premium",
        "home feature click",
    ):
        assert expected in journey

    assert "log is neither" in journey
    assert "an allow nor a denial" in journey
    assert "Do not use `qualification/tools/checker_verdict_exit.py` unchanged" in journey
    assert "ARTEMIS_TRACES_DIR" in journey
    assert "DATA_ENGINE_DB_PATH" in journey
    assert "artemis run --standalone --session-id" in journey
    assert "same-session" in journey
    assert "fail_assertion" in journey
    assert "corrected-build re-review on CHE-2015" in journey
    assert "NOT-RUN" in journey


def test_candidate_records_a_single_allowed_scenario_and_intended_negative_control():
    assert RECEIPT_PATH.is_file(), f"missing candidate receipt: {RECEIPT_PATH}"
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["positive_case"]["expected_events"] == [
        "show allowed: shown and dismissed",
        "revenue event for placement main",
        "show denied by Tinos rules (NOT_ALLOWED)",
    ]
    control = receipt["negative_control"]
    assert control["expected_verdict"] == "fail_assertion"
    assert control["false_assertion"] == (
        "The first home feature click must be denied by Tinos rules (NOT_ALLOWED)."
    )
    assert control["invalid_when_fill_fails"] is True
    assert control["requires_same_session_checker_capture"] is True
    assert receipt["review_and_execution_boundary"]["device_execution"] == "NOT-RUN"
    assert receipt["review_and_execution_boundary"]["device_claim"] == "not granted"
    assert any(
        "corrected-build re-review on CHE-2015" in condition
        for condition in receipt["review_and_execution_boundary"]["execution_requires"]
    )
