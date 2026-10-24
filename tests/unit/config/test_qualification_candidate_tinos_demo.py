"""Structural checks for CHE-2016's Tinos Artemis testcase candidate."""

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/tinos_demo_gated_interstitial.che2016.v1.json"
JOURNEY_PATH = REPO_ROOT / "qualification/journeys/tinos_demo_gated_interstitial.md"
TINOS_MANIFEST_PATH = (
    REPO_ROOT / "qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json"
)
SOURCE_MANIFEST_PATH = REPO_ROOT / "qualification/manifests/pocket_actual_save_relaunch.v1.json"
SOURCE_JOURNEY_PATH = REPO_ROOT / "qualification/journeys/pocket_actual_save_relaunch.md"
RUNNER_SHA = "feefd94c72dbce0fa1afc109ffe3d8254f565d6b"
TESTCASE_SOURCE_SHA = "096411113522abc37964256833fecbbac4778af4"
APP_SHA = "24d3ad51350767d04a2951ab72685a45f67a6f75"
APK_SHA256 = "b2a14979f173bc3e022a15aa5e3385085c8a02167ceda4b64b69da9e060efe64"
APK_SIZE_BYTES = 67183124
TEST_APP_ID = "ca-app-pub-3940256099942544~3347511713"
TEST_INTERSTITIAL = "ca-app-pub-3940256099942544/1033173712"
REVENUE_EXPECTATION = (
    f"revenue <micros> <currency> unit={TEST_INTERSTITIAL} network=<network> precision=<precision>"
)


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
    assert testcase["format_reference_only"] is True
    assert testcase["manifest_path"] == str(SOURCE_MANIFEST_PATH.relative_to(REPO_ROOT))
    assert (
        testcase["manifest_digest_sha256"]
        == hashlib.sha256(SOURCE_MANIFEST_PATH.read_bytes()).hexdigest()
    )

    manifest_bytes = SOURCE_MANIFEST_PATH.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == testcase["manifest_digest_sha256"]
    assert json.loads(manifest_bytes)["manifest_version"] == 9
    assert json.loads(manifest_bytes)["fork_sha"]["value"] == RUNNER_SHA
    source_journey_bytes = SOURCE_JOURNEY_PATH.read_bytes()
    assert hashlib.sha256(source_journey_bytes).hexdigest() == testcase["journey_digest_sha256"]

    app = source["app_input_source"]
    assert app == {
        "repository": "https://github.com/tevo-studio/sa049-dual-app",
        "pull_request": 145,
        "commit_sha": APP_SHA,
    }
    assert source["app_sha"] == APP_SHA
    assert source["apk_digest_sha256"] == APK_SHA256
    assert source["apk_size_bytes"] == APK_SIZE_BYTES
    assert f"{APK_SIZE_BYTES} bytes" in source["apk_size_note"]
    assert "Sol has not independently hashed" in source["apk_size_note"]
    assert source["package_name"] == "com.dualix.dualmessenger.webscanner"
    assert source["google_test_ad_app_id"] == TEST_APP_ID
    assert receipt["testcase"]["journey_path"] == str(JOURNEY_PATH.relative_to(REPO_ROOT))
    assert (
        receipt["testcase"]["journey_digest_sha256"]
        == hashlib.sha256(JOURNEY_PATH.read_bytes()).hexdigest()
    )
    assert receipt["testcase"]["manifest_path"] == str(TINOS_MANIFEST_PATH.relative_to(REPO_ROOT))
    assert (
        receipt["testcase"]["manifest_digest_sha256"]
        == hashlib.sha256(TINOS_MANIFEST_PATH.read_bytes()).hexdigest()
    )


def test_tinos_manifest_pins_profile_dependencies_revision_and_full_cli_contract():
    assert TINOS_MANIFEST_PATH.is_file(), f"missing Tinos manifest: {TINOS_MANIFEST_PATH}"
    manifest = json.loads(TINOS_MANIFEST_PATH.read_text(encoding="utf-8"))

    app = manifest["candidate_app"]
    assert app["app_sha"] == APP_SHA
    assert app["apk_digest_sha256"] == APK_SHA256
    assert app["apk_size_bytes"] == APK_SIZE_BYTES
    assert f"{APK_SIZE_BYTES} bytes" in app["apk_size_note"]
    assert "Sol has not independently hashed" in app["apk_size_note"]

    assert manifest["manifest_version"] == 1
    assert manifest["fork_sha"]["value"] == RUNNER_SHA
    assert manifest["reference_source"] == {
        "repository": "https://github.com/cheese-work/artemis",
        "commit_sha": TESTCASE_SOURCE_SHA,
        "manifest_version": 9,
        "format_reference_only": True,
    }
    assert manifest["dependency_lock"] == {
        "file": "uv.lock",
        "digest_sha256": "2c63863dd5827482d7e206a887219c6438ce61dc494d9cc596f862378decc19a",
        "runner_sha": RUNNER_SHA,
    }
    assert manifest["profile"] == "pro"
    assert "No new spend is authorized" in manifest["spend_policy"]
    entry_point = manifest["entry_point"]
    assert entry_point["route"] == "cli"
    command = entry_point["command_template"]
    for argument in (
        'artemis run "<case-goal>"',
        "--profile pro",
        "--locked-app com.dualix.dualmessenger.webscanner",
        "--test-name <case-id>",
        "--traces-path <attempt-root>/artemis-traces",
        "--device-serial <Sol-granted-exact-serial>",
        "--session-id <fresh-UUID>",
        "--run-id <same-fresh-UUID>",
        "--enable-checker",
        "--verification-level final",
        "--with-video-recording-tools",
        "--standalone",
    ):
        assert argument in command
    assert "..." not in command
    assert set(entry_point["case_goals"]) == {
        "tinos-demo-allow-then-interval-denial",
        "tinos-demo-first-click-negative-control",
    }
    assert "revision_policy" in manifest


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
        "Premium paywall",
        "one second",
        "Direct Message",
        "home-screen-2-feature-direct_message",
        "Android system Back",
        "first exact allow-log timestamp",
        "15 seconds",
        "visible duration in seconds",
        "`67183124` bytes",
        "15 seconds - D",
        "(t2 - t1) + D + 5 seconds <= 20 seconds",
        "up to 4 seconds",
        "not dismissal",
        "±1 second",
        "close control within 15 seconds",
        "show and close timestamps from the recording",
        "PR #100's merge is not the runner pin",
        "b219d55",
        "NOT-RUN",
        "revenue <micros> <currency> unit=<adUnitId> network=<network> precision=<precision>",
        f"unit={TEST_INTERSTITIAL}",
    ):
        assert expected in journey

    assert "neither an allow nor a denial" in journey
    assert "Do not apply the Pocket Actual checker adapter unchanged to Tinos." in journey
    assert "ARTEMIS_TRACES_DIR" in journey
    assert "DATA_ENGINE_DB_PATH" in journey
    assert "qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json" in journey
    assert "same session" in journey
    assert "fail_assertion" in journey
    assert "app-source PASS on CHE-2015" in journey
    assert "changes only the SDK pin and repo-local Maven artifacts" in journey
    assert "earlier binary evidence does not transfer" in journey
    assert journey.count("close control within 15 seconds") >= 2
    assert "NOT-RUN" in journey


def test_candidate_records_a_single_allowed_scenario_and_intended_negative_control():
    assert RECEIPT_PATH.is_file(), f"missing candidate receipt: {RECEIPT_PATH}"
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    assert receipt["positive_case"]["expected_events"] == [
        "show allowed: shown and dismissed",
        REVENUE_EXPECTATION,
        "show denied by Tinos rules (NOT_ALLOWED)",
    ]
    assert "placement" not in receipt["positive_case"]["expected_events"][1]
    assert receipt["positive_case"]["missing_revenue_result"] == (
        "FAIL: revenue_event_missing; the revenue outcome is not a pass."
    )
    control = receipt["negative_control"]
    assert control["expected_verdict"] == "fail_assertion"
    assert control["false_assertion"] == (
        "The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED)."
    )
    assert control["invalid_when_fill_fails"] is True
    assert "close control within 15 seconds" in receipt["fixture"]["interstitial_close"]

    timing = receipt["positive_case"]["second_click_timing"]
    assert timing["max_visible_seconds"] == 15
    assert timing["elapsed_window_formula"] == "t2 - t1 <= 15 - D"
    assert timing["gate_check_bound_formula"] == "(t2 - t1) + D + 5 <= 20 seconds"
    assert timing["second_fill_load_max_seconds"] == 4
    assert timing["total_timestamp_uncertainty_seconds"] == 1
    assert timing["not_run_when_remaining_window_seconds"] == "15 - D <= 0"
    assert control["required_observations"] == [
        "show allowed: shown and dismissed",
        REVENUE_EXPECTATION,
    ]
    assert control["missing_revenue_result"] == "invalid_control"
    assert control["requires_same_session_checker_capture"] is True
    assert receipt["review_and_execution_boundary"]["device_execution"] == "NOT-RUN"
    assert receipt["review_and_execution_boundary"]["device_claim"] == "not granted"
    assert any(
        "signed app-source PASS on CHE-2015" in condition
        and "earlier binary evidence does not transfer" in condition
        for condition in receipt["review_and_execution_boundary"]["execution_requires"]
    )
