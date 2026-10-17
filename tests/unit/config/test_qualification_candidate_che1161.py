import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_PATH = REPO_ROOT / "qualification/candidates/che1161_x99_api35.v1.json"


def test_candidate_pins_reviewable_x99_qualification_arms_and_blocked_control():
    assert CANDIDATE_PATH.is_file(), "CHE-1161 qualification candidate is missing"
    candidate = json.loads(CANDIDATE_PATH.read_text(encoding="utf-8"))

    assert candidate["issue"] == "CHE-1161"
    assert candidate["runner_source"]["commit_sha"] == (
        "cbf2fea84cbd47ea54053239679ab521b01d4d23"
    )
    assert candidate["baseline_pilot"]["published_head"] == (
        "e0258131c9e52553169368b8b1e1b284de74df99"
    )
    assert candidate["baseline_pilot"]["testcase_digest_sha256"] == (
        "b01d8b8f2f4460dc7b8ed1231bf16ed445104673ad33bd2b39042d1a8c419176"
    )

    testcase = candidate["testcase_source"]
    testcase_bytes = (REPO_ROOT / testcase["path"]).read_bytes()
    assert hashlib.sha256(testcase_bytes).hexdigest() == testcase["digest_sha256"]

    arms = {arm["id"]: arm for arm in candidate["qualification_arms"]}
    assert set(arms) == {"clean_pass", "clean_rerun", "negative_control"}
    for arm_id in ("clean_pass", "clean_rerun"):
        assert arms[arm_id]["expected_verdict"] == "pass"
        assert arms[arm_id]["attempt_count"] == 1
        assert arms[arm_id]["retry_count"] == 0

    negative = arms["negative_control"]
    assert negative["expected_verdict"] == "fail_assertion"
    assert negative["expected_process_exit"] == "nonzero"
    assert negative["execution_status"] == "BLOCKED_SOURCE_GAP"
    assert negative["missing_path"] == "qualification/tools/checker_verdict_exit.py"
    assert negative["executable_command"] is None

    review = candidate["review_requirement"]
    assert review["independent_non_author_decisions"] == 2
    assert review["same_revision"] is True
    assert review["selected_by"] == "x99-codex-sol"
    assert candidate["qualification_ready"] is False
    assert candidate["execution_status"] == "NOT-RUN"
