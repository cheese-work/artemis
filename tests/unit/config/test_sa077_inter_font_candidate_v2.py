"""CHE-6729 admission pins; the original rendering contract stays immutable."""

import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]
STEM = "sa077_inter_font_rendering.che3386"


def test_current_admission_candidate_preserves_rendering_contract():
    old = json.loads((ROOT / f"qualification/candidates/{STEM}.v1.json").read_text())
    candidate = json.loads((ROOT / f"qualification/candidates/{STEM}.v2.json").read_text())
    source = candidate["source_candidate"]
    app = source["app_input_source"]
    assert app["commit_sha"] == "b7de1b949aada147290d7ad48f75b50c851ae3d1"
    assert candidate["issue"] == "CHE-6729"
    assert candidate["parent_issue"] == old["parent_issue"] == "CHE-3385"
    for key in ("runner_source", "checker_adapter"):
        assert source[key] == old["source_candidate"][key]
    for key in ("target", "execution_boundary", "candidate_kind", "receipt_version"):
        assert candidate[key] == old[key]
    assert app["repository"] == old["source_candidate"]["app_input_source"]["repository"]
    assert app["package_name"] == old["source_candidate"]["app_input_source"]["package_name"]
    apk_hash = app["apk_digest_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", apk_hash)
    assert apk_hash != old["source_candidate"]["app_input_source"]["apk_digest_sha256"]
    journey_path = f"qualification/journeys/{STEM}.v2.md"
    assert source["testcase_source"]["path"] == journey_path
    journey_bytes = (ROOT / journey_path).read_bytes()
    assert source["testcase_source"]["digest_sha256"] == hashlib.sha256(journey_bytes).hexdigest()
    journey = journey_bytes.decode()
    assert app["commit_sha"] in journey
    assert apk_hash in journey
    original = (ROOT / f"qualification/journeys/{STEM}.v1.md").read_text()
    boundary = "## Repeatable fixture and cleanup"
    assert journey.split(boundary, 1)[1] == original.split(boundary, 1)[1]


if __name__ == "__main__":
    test_current_admission_candidate_preserves_rendering_contract()
    print("CHE-6729 current admission candidate checks passed")
