"""Structural checks for CHE-3386's pinned bundled-Inter testcase."""

import hashlib
import json
from pathlib import Path


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


if __name__ == "__main__":
    test_receipt_pins_runner_app_and_exact_journey_bytes()
    test_journey_covers_every_direct_layout_and_both_picker_surfaces()
    test_journey_defines_synthetic_fixture_reset_and_evidence_contract()
    print("CHE-3386 candidate checks passed")
