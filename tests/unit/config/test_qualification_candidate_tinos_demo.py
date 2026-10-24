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
    f"revenue <valueMicros> micros <currency> unit={TEST_INTERSTITIAL} "
    "network=<network> precision=<precision>"
)


def _submitted_argv(manifest, case_id):
    attempt_uuid = "00000000-0000-4000-8000-000000000001"
    replacements = {
        "<case-goal>": manifest["entry_point"]["case_goals"][case_id],
        "<case-id>": case_id,
        "<attempt-root>": "/synthetic/attempt",
        "<Sol-granted-exact-serial>": "SYNTHETIC-ONLY-NO-DEVICE",
        "<fresh-UUID>": attempt_uuid,
        "<same-fresh-UUID>": attempt_uuid,
    }
    argv = []
    for argument in manifest["entry_point"]["command_argv_template"]:
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        argv.append(argument)
    return argv


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
    manifest = json.loads(TINOS_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert (
        receipt["execution"]["runner_command_argv_template"]
        == manifest["entry_point"]["command_argv_template"]
    )
    assert (
        receipt["execution"]["log_capture_argv_template"]
        == manifest["entry_point"]["log_capture_argv_template"]
    )
    assert (
        receipt["execution"]["verdict_adapter_argv_templates"]
        == manifest["entry_point"]["post_run_verifier_argv_templates"]
    )
    assert receipt["execution"]["verdict_adapter"]["runner_change"] is False
    assert receipt["execution"]["log_capture_tool"]["runner_change"] is False
    assert receipt["positive_case"]["scripted_oracle"]["runner_change"] is False
    assert receipt["positive_case"]["scripted_oracle"]["inputs"] == [
        "first-visible epoch timestamp from the recording",
        "close-control epoch timestamp from the recording",
    ]
    assert receipt["negative_control"]["required_feature_tap_count"] == 1
    assert receipt["negative_control"]["reject_if_denial_present"]
    assert "MIN(timestamp)..MAX(timestamp)" in receipt["negative_control"]["event_time_validation"]
    assert "full clock-uncertainty interval" in receipt["negative_control"]["event_time_validation"]
    adapter_path = REPO_ROOT / receipt["execution"]["verdict_adapter"]["path"]
    assert (
        receipt["execution"]["verdict_adapter"]["script_sha256"]
        == hashlib.sha256(adapter_path.read_bytes()).hexdigest()
    )
    capture_path = REPO_ROOT / receipt["execution"]["log_capture_tool"]["path"]
    assert (
        receipt["execution"]["log_capture_tool"]["script_sha256"]
        == hashlib.sha256(capture_path.read_bytes()).hexdigest()
    )
    assert (
        receipt["execution"]["log_capture_tool"]["clock_mapping"]["maximum_uncertainty_seconds"]
        == 1
    )
    assert "SIGKILL can bypass" in receipt["execution"]["log_capture_tool"]["pid_policy"]
    assert (
        "confirm the recorded logger PID is gone"
        in receipt["reporting"]["logger_release_requirement"]
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
    assert entry_point["environment_template"] == {
        "ARTEMIS_TRACES_DIR": "<attempt-root>/ledger",
        "DATA_ENGINE_DB_PATH": "<attempt-root>/ledger/data_engine.db",
    }
    command_argv = entry_point["command_argv_template"]
    assert command_argv[:3] == ["artemis", "run", "<case-goal>"]
    assert command_argv[3:] == [
        "--profile",
        "pro",
        "--locked-app",
        "com.dualix.dualmessenger.webscanner",
        "--test-name",
        "<case-id>",
        "--traces-path",
        "<attempt-root>/artemis-traces",
        "--device-serial",
        "<Sol-granted-exact-serial>",
        "--session-id",
        "<fresh-UUID>",
        "--run-id",
        "<same-fresh-UUID>",
        "--enable-checker",
        "--verification-level",
        "final",
        "--with-video-recording-tools",
        "--standalone",
    ]
    capture_argv = entry_point["log_capture_argv_template"]
    assert capture_argv[:6] == [
        "bash",
        "qualification/tools/capture_tinos_demo_logs.sh",
        "<Sol-granted-exact-serial>",
        "<attempt-root>/tinos-demo-logcat.txt",
        "<attempt-root>/tinos-demo-logcat.meta",
        "--",
    ]
    assert capture_argv[6:] == command_argv
    assert entry_point["capture_clock_policy"]["maximum_uncertainty_seconds"] == 1
    assert "full uncertainty interval" in entry_point["capture_clock_policy"]["event_rule"]
    assert set(entry_point["case_goals"]) == {
        "tinos-demo-allow-then-interval-denial",
        "tinos-demo-first-click-negative-control",
    }
    verifier_templates = entry_point["post_run_verifier_argv_templates"]
    assert set(verifier_templates) == {"positive", "control"}
    assert verifier_templates["positive"] == [
        "python",
        "qualification/tools/tinos_demo_verdict_exit.py",
        "--mode",
        "positive",
        "--traces-dir",
        "<attempt-root>/ledger",
        "--data-engine-db",
        "<attempt-root>/ledger/data_engine.db",
        "--session-id",
        "<same-fresh-UUID>",
        "--logcat-file",
        "<attempt-root>/tinos-demo-logcat.txt",
        "--capture-metadata-file",
        "<attempt-root>/tinos-demo-logcat.meta",
        "--device-serial",
        "<Sol-granted-exact-serial>",
        "--runner-exit-status",
        "<observed-artemis-exit-status>",
        "--first-visible-at",
        "<first-visible-epoch>",
        "--closed-at",
        "<closed-epoch>",
    ]
    assert verifier_templates["control"][3] == "control"
    assert verifier_templates["control"][-2:] == [
        "--runner-exit-status",
        "<observed-artemis-exit-status>",
    ]
    assert entry_point["testcase_side_tool"]["runner_change"] is False
    assert entry_point["testcase_side_log_capture"]["runner_change"] is False
    assert "revision_policy" in manifest


def test_submitted_cli_goal_payload_contains_reviewed_scenario_assertions():
    manifest = json.loads(TINOS_MANIFEST_PATH.read_text(encoding="utf-8"))
    positive_id = "tinos-demo-allow-then-interval-denial"
    control_id = "tinos-demo-first-click-negative-control"
    positive_argv = _submitted_argv(manifest, positive_id)
    control_argv = _submitted_argv(manifest, control_id)
    positive_goal = positive_argv[2]
    control_goal = control_argv[2]

    assert positive_goal == manifest["entry_point"]["case_goals"][positive_id]
    assert control_goal == manifest["entry_point"]["case_goals"][control_id]
    assert positive_goal != control_goal
    assert "qualification/journeys/tinos_demo_gated_interstitial.md" not in positive_goal
    assert "qualification/journeys/tinos_demo_gated_interstitial.md" not in control_goal
    assert "show allowed: shown and dismissed" in positive_goal
    assert "show allowed: shown and dismissed" in control_goal
    assert "exactly two tap timestamps from this same session's steps rows" in positive_goal
    assert "exactly one tap timestamp from this same session's steps rows" in control_goal
    assert "first-visible and close-control timestamps from the recording" in positive_goal
    assert "first-visible and close-control timestamps from the recording" in control_goal
    assert REVENUE_EXPECTATION in positive_goal
    assert REVENUE_EXPECTATION in control_goal
    assert "(t2 - t1) + D + 5 seconds <= 20 seconds" in positive_goal
    assert "t2 - t1 <= 15 seconds - D" in positive_goal
    assert "gate_allowed_inside_interval" in positive_goal
    false_assertion = (
        "The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED)."
    )
    assert false_assertion not in positive_goal
    assert false_assertion in control_goal
    assert (
        positive_argv[positive_argv.index("--session-id") + 1]
        == positive_argv[positive_argv.index("--run-id") + 1]
    )
    assert (
        control_argv[control_argv.index("--session-id") + 1]
        == control_argv[control_argv.index("--run-id") + 1]
    )


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
        "full uncertainty interval is inside",
        "device epoch cursor",
        "PR #100's merge is not the runner pin",
        "b219d55",
        "NOT-RUN",
        "revenue <valueMicros> micros <currency> unit=<adUnitId> network=<network> precision=<precision>",
        f"unit={TEST_INTERSTITIAL}",
    ):
        assert expected in journey

    assert "neither an allow nor a denial" in journey
    assert "Do not apply the Pocket Actual checker adapter unchanged to Tinos." in journey
    assert "ARTEMIS_TRACES_DIR" in journey
    assert "DATA_ENGINE_DB_PATH" in journey
    assert "qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json" in journey
    assert "same session" in journey
    assert "tinos_demo_verdict_exit.py" in journey
    assert "testcase-side tool, not a runner change" in journey
    assert "only when Artemis exited `0`" in journey
    assert "fail_assertion" in journey
    assert "app-source PASS on CHE-2015" in journey
    assert "changes only the SDK pin and repo-local Maven artifacts" in journey
    assert "earlier binary evidence does not transfer" in journey
    assert journey.count("close control within 15 seconds") >= 2
    assert "NOT-RUN" in journey

    positive_case = journey.split("## Positive case: allow, then deny", 1)[1].split(
        "## Negative control: false first-click denial", 1
    )[0]
    assert positive_case.count("Return to Home exactly once with Android system Back") == 1
    assert (
        "`t2` from the second matching tap row in this same session after step 3" in positive_case
    )
    assert (
        "allow line is absent after a close within 15 seconds, report that attempt `NOT-RUN`"
        in journey
    )
    assert "show wait starts when it calls `show`, not when the first frame is visible" in journey
    assert "reads exactly two feature-tap timestamps from this session's `steps` rows" in journey
    assert "reads `first_visible_at` and `closed_at` from the recording" in journey
    assert "SIGKILL" in journey
    assert "confirm the logger PID is gone before device release" in journey


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
    assert receipt["positive_case"]["gate_failure_result"] == (
        "FAIL: gate_allowed_inside_interval with two allow lines and no denial inside the "
        "verified timing window; record revenue_observation separately as present, missing, "
        "or malformed."
    )
    assert receipt["fixture"]["tap_timestamp_source"].startswith(
        "The verifier derives matching tap action timestamps from the same session's steps rows"
    )
    assert "SIGKILL can bypass" in receipt["fixture"]["sigkill_logger_limit"]
    control = receipt["negative_control"]
    assert control["expected_verdict"] == "fail_assertion"
    assert control["false_assertion"] == (
        "The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED)."
    )
    assert control["invalid_when_fill_fails"] is True
    assert "close control within 15 seconds" in receipt["fixture"]["interstitial_close"]
    assert (
        "allow line is absent after a close within 15 seconds"
        in receipt["fixture"]["allow_log_timeout_race"]
    )
    assert "NOT-RUN" in receipt["fixture"]["allow_log_timeout_race"]
    assert "not when the first frame is visible" in receipt["fixture"]["allow_log_timeout_race"]
    assert receipt["positive_case"]["missing_allow_result"].startswith("NOT-RUN:")
    assert receipt["negative_control"]["missing_allow_result"].startswith("NOT-RUN:")

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
    assert control["missing_revenue_result"] == "invalid_control: revenue_event_missing"
    assert control["requires_same_session_checker_capture"] is True
    assert receipt["review_and_execution_boundary"]["device_execution"] == "NOT-RUN"
    assert receipt["review_and_execution_boundary"]["device_claim"] == "not granted"
    assert any(
        "signed app-source PASS on CHE-2015" in condition
        and "earlier binary evidence does not transfer" in condition
        for condition in receipt["review_and_execution_boundary"]["execution_requires"]
    )
