"""Structural checks for the CHE-2927 mobile chat testcase candidate."""

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE = REPO_ROOT / "qualification/candidates/che2927_mobile_chat_steer.v1.json"


def test_candidate_pins_sources_and_covers_all_states_in_both_themes():
    candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    source = candidate["source_candidate"]
    assert candidate["issue"] == "CHE-2927"
    assert source["runner_source"]["commit_sha"] == "feefd94c72dbce0fa1afc109ffe3d8254f565d6b"
    assert source["app_input_source"]["commit_sha"] == "c6812181ab042ec395e62ea14d98cc7f1f5f26e3"
    assert source["server_contract_source"]["commit_sha"] == "272a5996013d65be5014ab8172da559f84a06bac"

    testcase = source["testcase_source"]
    journey = REPO_ROOT / testcase["journey_path"]
    assert hashlib.sha256(journey.read_bytes()).hexdigest() == testcase["journey_digest_sha256"]

    cases = {case["id"]: case for case in candidate["coverage"]}
    assert set(cases) == {
        "long_queue_keyboard",
        "steer_capable_head",
        "fallback_starting",
        "fallback_unsupported",
        "supplement_added",
        "supplement_read",
        "supplement_not_received",
        "supplement_uncertain_confirm",
    }
    assert all(set(case["screenshots_by_theme"]) == {"light", "dark"} for case in cases.values())
    assert candidate["review_scope"]["same_revision_required"] is True
    assert len(candidate["review_scope"]["required_reviewers"]) == 2
    assert candidate["execution_boundary"]["device_or_adb_actions"] is False
    assert candidate["execution_boundary"]["artemis_execution"] is False
