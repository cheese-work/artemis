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
    assert fork_sha == "64e1b3226b9553bdf4e5a368f14b8a40c0af6111", (
        "fork_sha must match the CHE-537 observed source baseline; if the pin "
        "legitimately moved, this test and FORK_MAINTENANCE.md's promotion "
        "record must be updated together, not silently."
    )


def test_candidate_app_fields_are_a_hold_not_a_default():
    manifest = _load_manifest()
    candidate = manifest["candidate_app"]
    assert candidate["app_sha"] is None
    assert candidate["apk_digest_sha256"] is None
    assert candidate["package_name"] is None


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


def test_journey_defines_fixture_reset_and_negative_control():
    text = JOURNEY_PATH.read_text(encoding="utf-8")
    assert "pm clear" in text
    assert "force-stop" in text
    assert "Negative control" in text
    assert "WRONG-SUFFIX" in text
