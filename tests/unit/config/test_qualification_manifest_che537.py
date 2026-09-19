# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Structural checks for the CHE-537 pinned qualification manifest.

These are plain dict/field assertions, not JSON Schema validation (no
`jsonschema` dependency introduced for one manifest). The manifest and its
schema doc are authored, not executed, by this issue -- these tests only
guard against the manifest silently drifting out of the shape the
qualification execution stage (CHE-540+) and the Gate 1 tier vocabulary
(PR #1 / CHE-491) depend on.
"""

import json
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "qualification/manifests/pocket_actual_save_relaunch.v1.json"
JOURNEY_PATH = REPO_ROOT / "qualification/journeys/pocket_actual_save_relaunch.md"
EVIDENCE_SCHEMA_PATH = REPO_ROOT / "qualification/evidence_schema.v1.json"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Must stay in lockstep with artemis/config/attempt_manifest.py's TIER_MODELS
# (PR #1 / CHE-491). This test intentionally hardcodes the same tuple rather
# than importing that (unmerged, draft) module, so it fails loudly if this
# manifest's tiers/providers/models drift from what Gate 1 actually declares.
_KNOWN_TIER_MODELS = {
    "luna": ("google", "gemini-3.5-flash-lite"),
    "terra": ("google", "gemini-3.8-flash"),
    "sol": ("openai", "gpt-5.6-sol"),
}


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_and_companion_files_exist():
    assert MANIFEST_PATH.is_file()
    assert JOURNEY_PATH.is_file()
    assert EVIDENCE_SCHEMA_PATH.is_file()


def test_fork_sha_is_a_valid_git_sha_and_matches_source_baseline():
    manifest = _load_manifest()
    fork_sha = manifest["fork_sha"]["value"]
    assert _SHA_RE.match(fork_sha), f"fork_sha must be a 40-hex-char git SHA, got {fork_sha!r}"
    assert fork_sha == "144006b1b2e6888e31dc8d76887a7da5e8839e99", (
        "fork_sha must match the CHE-672 promoted pin (advanced past CHE-491 "
        "per the revision_policy); if the pin legitimately moves again, this "
        "test and FORK_MAINTENANCE.md's promotion record must be updated "
        "together, not silently."
    )


def test_candidate_app_fields_are_pinned_by_che540():
    """CHE-537 left these fields null as a hold; CHE-540 (manifest revision v2)
    pins the exact pocket-actual build under test. If the pin legitimately
    moves, this test and the manifest's candidate_app.note must be updated
    together (revision_policy: a changed app_sha/apk_digest_sha256 forces a
    new manifest revision and invalidates prior evidence).
    """
    manifest = _load_manifest()
    candidate = manifest["candidate_app"]
    assert _SHA_RE.match(candidate["app_sha"]), (
        f"app_sha must be a 40-hex-char git SHA, got {candidate['app_sha']!r}"
    )
    assert candidate["app_sha"] == "2c42481778e6f9b88a9502f55decdbe4e1a76e83"
    assert _SHA256_RE.match(candidate["apk_digest_sha256"]), (
        f"apk_digest_sha256 must be a 64-hex-char sha256 digest, got {candidate['apk_digest_sha256']!r}"
    )
    assert candidate["apk_digest_sha256"] == (
        "04795d5f3995c8e895f6440a9763c8a003c8b6ff955541e3965f5e71564e2e12"
    )
    assert candidate["package_name"] == "dev.cheese.pocketactual"


def test_dependency_lock_digest_is_pinned_by_che540():
    manifest = _load_manifest()
    digest = manifest["dependency_lock"]["digest_sha256"]
    assert _SHA256_RE.match(digest), (
        f"digest_sha256 must be a 64-hex-char sha256 digest, got {digest!r}"
    )
    assert digest == "2c63863dd5827482d7e206a887219c6438ce61dc494d9cc596f862378decc19a"


def test_model_targets_match_gate1_tier_vocabulary():
    manifest = _load_manifest()
    targets = manifest["model_targets"]
    assert len(targets) >= 2, "CHE-388 requires baselining at least two model tiers"
    for target in targets:
        tier = target["tier"]
        assert tier in _KNOWN_TIER_MODELS, f"unknown tier {tier!r} not in Gate 1 TIER_MODELS"
        expected_provider, expected_model = _KNOWN_TIER_MODELS[tier]
        assert target["provider"] == expected_provider
        assert target["model"] == expected_model
        assert target["provider_id"] == f"{expected_provider}:{expected_model}"


def test_model_targets_are_luna_and_terra_per_che388_plan_not_sol():
    manifest = _load_manifest()
    tiers = {t["tier"] for t in manifest["model_targets"]}
    assert tiers == {"luna", "terra"}, (
        "CHE-388's plan text says 'compare Luna and Terra model tiers'; "
        "'sol' resolves to an OpenAI-backed model and the workspace announcement "
        "bars all OpenAI routing indefinitely, so it must be excluded here, not pinned."
    )
    excluded = manifest["sol_tier_excluded"]
    assert excluded["tier"] == "sol"
    assert "unblock_condition" in excluded


def test_fixture_counts_match_che388_plan_v2():
    manifest = _load_manifest()
    fixture = manifest["fixture"]
    assert fixture["positive_runs_required"] == 10
    assert fixture["negative_controls_required"] == 1


def test_revision_policy_and_review_contract_present():
    manifest = _load_manifest()
    assert "rule" in manifest["revision_policy"]
    review = manifest["review_contract"]
    assert review["no_self_review"] is True
    assert "Opus AND Sol" in review["chain_as_originally_settled"]
    assert "mbp-claude-sonnet-5" in review["candidate_reviewer_this_issue"]


def test_evidence_schema_declares_verdict_enum_covering_infra_and_device_failures():
    schema = json.loads(EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    verdict_enum = set(schema["properties"]["verdict"]["enum"])
    assert verdict_enum == {"pass", "fail_assertion", "fail_infrastructure", "device_unavailable"}


def test_evidence_schema_reject_reasons_match_gate1_reconciliation_vocabulary():
    schema = json.loads(EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    reject_reasons = set(
        schema["properties"]["reconciliation"]["properties"]["reject_reasons"]["items"]["enum"]
    )
    # Mirrors artemis/config/attempt_reconciliation.py's REJECT_REASONS (PR #1 / CHE-491).
    assert reject_reasons == {
        "missing_identity",
        "unmapped_call",
        "mixed_tier",
        "unexpected_model_or_endpoint",
        "fake_mode_enabled",
        "digest_drift",
        "missing_snapshot",
    }


def test_evidence_schema_host_enum_includes_x99_and_macbook_pro():
    schema = json.loads(EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    host_enum = set(schema["properties"]["host"]["enum"])
    assert {"congvc-x99", "macbook-pro"} <= host_enum, (
        "CHE-388's 2026-09-18 scope correction restricts new Artemis hosts to "
        "congvc-x99 and macbook-pro (CHE-542/congvc-c00 cancelled)."
    )


def test_evidence_schema_requires_transport_distinct_from_host():
    schema = json.loads(EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert "transport" in schema["required"]
    assert set(schema["properties"]["transport"]["enum"]) == {"usb", "wireless_adb", "emulator"}, (
        "CHE-540's X99 AVD lane (e.g. emulator-5556) is neither usb nor "
        "wireless_adb; 'emulator' must stay representable or every CHE-540 "
        "evidence record becomes schema-invalid."
    )


def test_journey_defines_fixture_reset_and_negative_control():
    text = JOURNEY_PATH.read_text(encoding="utf-8")
    assert "pm clear" in text
    assert "force-stop" in text
    assert "Negative control" in text
    assert "WRONG-SUFFIX" in text
