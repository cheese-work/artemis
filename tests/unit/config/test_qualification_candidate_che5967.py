"""Contract checks for CHE-5967's pinned X99 AVD qualification candidate."""

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_PATH = REPO_ROOT / "qualification/candidates/che5967_x99_avd.v1.json"
RUNNER_SHA = "676320699514778d7b589783cb5590ada8d4048f"
SAMPLE_SHA = "649ae000ab7527105b6be5351f2bc536f9980bac"
APK_SHA256 = "38a9b0c53917c5cbf24f78625ac06d6e4a08e1bb1a8c0ac688c663884909f082"


def _candidate() -> dict:
    return json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))


def test_candidate_pins_runner_device_app_and_journey():
    candidate = _candidate()

    runner = candidate["runner_source"]
    assert runner["repository"] == "https://github.com/cheese-work/artemis"
    assert runner["commit_sha"] == RUNNER_SHA
    for source in runner["source_files"]:
        content = (REPO_ROOT / source["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == source["digest_sha256"]

    target = candidate["target"]
    assert target == {
        "device_class": "x99_api35_medium_phone",
        "system_image": "google_apis",
        "api": 35,
        "abi": "x86_64",
        "image_revision": 9,
        "serial": "assigned_at_execution",
    }

    app = candidate["app_build"]
    assert app["repository"] == "https://github.com/tevo-studio/mMonetize-sample"
    assert app["commit_sha"] == SAMPLE_SHA
    assert app["build_id"] == "CHE-2705-649ae000ab"
    assert app["variant"] == "adsTestDebug"
    assert app["package_name"] == "io.mmonetize.sample"
    assert app["version_code"] == 1
    assert app["apk_sha256"] == APK_SHA256

    testcase = candidate["testcase_source"]
    journey = REPO_ROOT / testcase["path"]
    assert testcase["digest_sha256"] == hashlib.sha256(journey.read_bytes()).hexdigest()
    assert testcase["status"] == "provisional"


def test_journey_requires_clean_pass_rerun_negative_control_and_release():
    candidate = _candidate()
    qualification = candidate["qualification"]
    journey = (REPO_ROOT / candidate["testcase_source"]["path"]).read_text(encoding="utf-8")

    assert qualification["positive_run_count"] == 2
    assert qualification["negative_control"]["expected_native_verdict"] == "fail_assertion"
    assert qualification["negative_control"]["expected_process_exit"] == 1
    assert qualification["negative_control"]["incomplete_evidence_is_not_success"] is True
    assert qualification["required_artifacts"] == [
        "native test summary and process exit status for each run",
        "Artemis trace bundle for each run",
        "screenshots or accessibility hierarchy for each asserted screen",
        "device identity and APK SHA-256 verification",
        "cleanup and lease release evidence",
    ]
    assert qualification["cleanup_contract"]["verified_release_required"] is True
    assert qualification["cleanup_contract"]["uncertain_release"] == (
        "keep target unavailable and name recovery owner"
    )

    for required_text in (
        "two independent non-author reviews",
        "clean positive pass",
        "clean positive rerun",
        "deliberately false assertion",
        "ARTEMIS_ASSERTION_EXIT=1",
        "incomplete evidence",
        "preserve artifacts before cleanup",
        "verified lease release",
        "execution evidence: NOT-RUN",
    ):
        assert required_text in journey
