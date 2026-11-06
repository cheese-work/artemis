"""Structural checks for CHE-3386's pinned bundled-Inter testcase."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[3]
RECEIPT_PATH = REPO_ROOT / "qualification/candidates/sa077_inter_font_rendering.che3386.v1.json"
JOURNEY_PATH = REPO_ROOT / "qualification/journeys/sa077_inter_font_rendering.che3386.v1.md"

LAYOUTS = {
    "activity_address_search.xml",
    "activity_book_guide.xml",
    "activity_book_open.xml",
    "activity_face_reading_info.xml",
    "activity_onboarding.xml",
    "activity_personalized_chart.xml",
    "activity_premium.xml",
    "activity_profile.xml",
    "activity_tarot_card_detail.xml",
    "activity_tarot_library.xml",
    "activity_tarot_question.xml",
    "activity_tarot_result.xml",
    "fragment_tarot_home.xml",
    "item_book_step.xml",
    "item_premium.xml",
    "item_tarot_library_card.xml",
    "item_tarot_section.xml",
    "native_full.xml",
    "native_no_media_loading.xml",
}
REQUIRED_RENDERING_ASSERTIONS = (
    "CHE3386_VISIBLE_CONTENT",
    "CHE3386_NO_CLIPPING",
    "CHE3386_DATE_PICKER_INTER_WEIGHTS",
    "CHE3386_PROFILE_PICKER_INTER_VALUES",
    "CHE3386_SCREENSHOT_EACH_ROUTE",
)


def test_receipt_pins_runner_app_and_exact_journey_bytes():
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    source = receipt["source_candidate"]
    assert receipt["issue"] == "CHE-3386"
    assert receipt["parent_issue"] == "CHE-3385"
    runner = source["runner_source"]
    assert runner["repository"] == "https://github.com/cheese-work/artemis"
    assert runner["commit_sha"] == "a14514a4508c366344c367aa39f672454de9c38f"
    assert runner["identity"] == "ARTEMIS CLI via uv run artemis run"
    app = source["app_input_source"]
    assert app["repository"] == "https://github.com/tevo-studio/sa077-astrology-3"
    assert app["commit_sha"] == "05c98f602bc0a211381000db82487caef0d5cb8a"
    assert app["package_name"] == "com.horoscope.tarotreader.palmistry.facereader"
    assert app["apk_digest_sha256"] == (
        "1d2315b9004695a6f0bab6c1e82e703f1e5a6b29d10887c8d6e2a564f58aee02"
    )
    testcase = source["testcase_source"]
    assert testcase["path"] == str(JOURNEY_PATH.relative_to(REPO_ROOT))
    assert hashlib.sha256(JOURNEY_PATH.read_bytes()).hexdigest() == testcase["digest_sha256"]
    adapter = source["checker_adapter"]
    adapter_path = REPO_ROOT / adapter["path"]
    assert adapter["path"] == "qualification/tools/checker_negative_control_exit.py"
    assert hashlib.sha256(adapter_path.read_bytes()).hexdigest() == adapter["digest_sha256"]


def test_journey_covers_every_direct_layout_and_both_picker_surfaces():
    journey = JOURNEY_PATH.read_text(encoding="utf-8")
    coverage = journey.split("## Layout coverage", maxsplit=1)[1]
    rows = {
        line.split("|", maxsplit=2)[1].strip().strip("`")
        for line in coverage.splitlines()
        if line.startswith("|") and line.count("|") >= 3 and ".xml" in line
    }
    assert rows == LAYOUTS
    assert "WheelColumnView" in journey
    assert "ProfileNumberPickerView" in journey
    assert "nonempty" in journey.lower()
    assert "clipping" in journey.lower()
    assert "screenshot" in journey.lower()


def test_journey_defines_synthetic_fixture_reset_and_evidence_contract():
    journey = JOURNEY_PATH.read_text(encoding="utf-8")
    for required in (
        "synthetic",
        "repeatable",
        "reset",
        "cleanup",
        "device class",
        "BLOCKED",
        "NOT-RUN",
        "one screenshot per reached screen",
    ):
        assert required.lower() in journey.lower()


def test_journey_defines_positive_rerun_and_negative_control_execution_contract():
    journey = JOURNEY_PATH.read_text(encoding="utf-8")
    for required in (
        "one positive run, one clean rerun",
        "Negative-control goal:",
        "--profile pro --verification-level final",
        '--device-serial "<claimed-serial>"',
        'SESSION_ID="<fresh-session-uuid>"',
        "ARTEMIS_TRACES_DIR=<ledger-root>",
        "DATA_ENGINE_DB_PATH=<ledger-root>/data_engine.db",
        "<ledger-root>/<session-id>/check_ledger.jsonl",
        "checker_negative_control_exit.py",
        "--expect pass",
        "--expect negative-control",
        "MUST_FAIL_CHE3386_<run-id>",
        "exit 1",
        "exit 2",
        "Do not treat a completed task status or a generic runner exit as proof",
        "--output-description",
        "900s",
        "latest `final#N` attempt",
    ):
        assert required in journey

    positive_goal = journey.split("Positive goal, used for the first run", maxsplit=1)[1]
    positive_goal = positive_goal.split("Positive output description:", maxsplit=1)[0]
    for required in (
        "CHE3386_VISIBLE_CONTENT",
        "CHE3386_NO_CLIPPING",
        "CHE3386_DATE_PICKER_INTER_WEIGHTS",
        "CHE3386_PROFILE_PICKER_INTER_VALUES",
        "CHE3386_SCREENSHOT_EACH_ROUTE",
        "Select English and Horoscope during onboarding",
        "Artemis QA",
        "Do not use a real person's profile",
        "Do not start a camera flow, purchase, subscription, or sign-in",
    ):
        assert required in positive_goal

    negative_goal = journey.split("Negative-control goal:", maxsplit=1)[1]
    negative_goal = negative_goal.split("Negative-control output description:", maxsplit=1)[0]
    for required in (
        "seven navigation steps",
        "Artemis QA",
        "Do not use a real person's profile",
        "Do not start a camera flow, purchase, subscription, or sign-in",
        "CHE3386_DATE_PICKER_INTER_WEIGHTS",
        "CHE3386_PROFILE_PICKER_INTER_VALUES",
        "MUST_FAIL_CHE3386_<run-id>",
    ):
        assert required in negative_goal


def test_negative_control_requires_each_rendering_assertion_to_pass(tmp_path):
    token = "MUST_FAIL_CHE3386_run-123"
    records = [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": "Startup completed",
            "status": "passed",
        },
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{token} is visible",
            "status": "failed",
        },
    ]
    result = _adapter_result(tmp_path, records, "--expect", "negative-control", "--token", token)

    assert result.returncode == 2
    assert json.loads(result.stdout)["verdict"] == "invalid_control"


def test_positive_run_rejects_a_ledger_without_rendering_assertions(tmp_path):
    records = [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": "Startup completed",
            "status": "passed",
        },
    ]
    result = _adapter_result(tmp_path, records, "--expect", "pass")

    assert result.returncode == 2
    assert json.loads(result.stdout)["verdict"] == "invalid_control"


def _adapter_result(tmp_path, records, *args):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
    adapter = REPO_ROOT / "qualification/tools/checker_negative_control_exit.py"
    return subprocess.run(
        [sys.executable, str(adapter), "--ledger", str(ledger), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_checker_adapter_accepts_only_all_passed_latest_final_assertions(tmp_path):
    records = [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{label}: passed",
            "status": "passed",
        }
        for label in REQUIRED_RENDERING_ASSERTIONS
    ]
    result = _adapter_result(tmp_path, records, "--expect", "pass")

    assert result.returncode == 0
    assert json.loads(result.stdout)["verdict"] == "pass"


def test_checker_adapter_accepts_only_the_deliberate_negative_assertion_failure(tmp_path):
    token = "MUST_FAIL_CHE3386_run-123"
    records = [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{label}: passed",
            "status": "passed",
        }
        for label in REQUIRED_RENDERING_ASSERTIONS
    ] + [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{token} is visible",
            "status": "failed",
        },
    ]
    result = _adapter_result(tmp_path, records, "--expect", "negative-control", "--token", token)

    assert result.returncode == 1
    assert json.loads(result.stdout)["verdict"] == "fail_assertion"


def test_checker_adapter_rejects_an_unrelated_failed_assertion(tmp_path):
    token = "MUST_FAIL_CHE3386_run-123"
    records = [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{label}: {'failed' if label == REQUIRED_RENDERING_ASSERTIONS[0] else 'passed'}",
            "status": "failed" if label == REQUIRED_RENDERING_ASSERTIONS[0] else "passed",
        }
        for label in REQUIRED_RENDERING_ASSERTIONS
    ] + [
        {
            "attempt_id": "final#1",
            "kind": "assert",
            "item_text": f"{token} is visible",
            "status": "failed",
        },
    ]
    result = _adapter_result(tmp_path, records, "--expect", "negative-control", "--token", token)

    assert result.returncode == 2
    assert json.loads(result.stdout)["verdict"] == "invalid_control"


if __name__ == "__main__":
    test_receipt_pins_runner_app_and_exact_journey_bytes()
    test_journey_covers_every_direct_layout_and_both_picker_surfaces()
    test_journey_defines_synthetic_fixture_reset_and_evidence_contract()
    test_journey_defines_positive_rerun_and_negative_control_execution_contract()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        test_checker_adapter_accepts_only_all_passed_latest_final_assertions(temp_path)
        test_checker_adapter_accepts_only_the_deliberate_negative_assertion_failure(temp_path)
        test_checker_adapter_rejects_an_unrelated_failed_assertion(temp_path)
        test_negative_control_requires_each_rendering_assertion_to_pass(temp_path)
        test_positive_run_rejects_a_ledger_without_rendering_assertions(temp_path)
    print("CHE-3386 candidate checks passed")
