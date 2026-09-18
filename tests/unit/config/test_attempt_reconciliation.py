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

"""Gate 1 mechanism tests: llm_usage <-> manifest reconciliation and the
seven-reason batch accept/reject rule.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from artemis.config.attempt_manifest import (
    build_attempt_manifest,
    digest_of,
    store_attempt_manifest,
)
from artemis.config.attempt_reconciliation import (
    AttemptRecord,
    REJECT_REASONS,
    reconcile_attempt,
    reconcile_attempt_batch_by_run_id,
    reconcile_finished_attempt,
    validate_batch,
)
from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback
from artemis.data_engine.models import SessionMetadata, TraceRecord
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store


def _llm(provider: str = "openai", model: str = "gpt-5.6-sol") -> LLMWithFallback:
    return LLMWithFallback(
        provider=provider, model=model, fallback=LLM(provider=provider, model=model)
    )


_REQUIRED_NODES = (
    "planner",
    "summarizer",
    "operator",
    "operator_summarizer",
    "log_reader_sub_agent",
    "log_analyzer",
    "diagnoser",
    "checker",
    "planner_avatar",
    "history_analyzer_expert",
    "diagnoser_expert",
    "explorer",
)


def _uniform_config(provider: str = "openai", model: str = "gpt-5.6-sol") -> LLMConfig:
    """A fully-populated LLMConfig where every required node resolves to one tier."""
    node = _llm(provider, model)
    return LLMConfig(
        **{n: node for n in _REQUIRED_NODES},
        utils=LLMConfigUtils(outputter=node, hopper=node),
    )


def _sol_config() -> LLMConfig:
    return _uniform_config()


def _manifest(**overrides):
    kwargs = dict(
        run_id="r",
        attempt_id="a1",
        trace_id="t1",
        tier="sol",
        llm_config=_sol_config(),
        env={},
        checkpoint="launch",
        now=1.0,
    )
    kwargs.update(overrides)
    return build_attempt_manifest(**kwargs)


def _usage(node: str, source: str | None = "openai:gpt-5.6-sol") -> dict:
    event = {"node": node, "prompt_tokens": 10, "completion_tokens": 5}
    if source is not None:
        event["source"] = source
    return event


class TestReconciliation:
    def test_matching_source_is_match(self):
        manifest = _manifest()
        result = reconcile_attempt("a1", manifest, [_usage("planner")])
        planner = next(n for n in result.nodes if n.node == "planner")
        assert planner.verdict == "match"
        assert not result.has_mismatch

    def test_mismatched_source_flags_mismatch(self):
        manifest = _manifest()
        result = reconcile_attempt(
            "a1", manifest, [_usage("planner", source="google:gemini-3.5-flash-lite")]
        )
        planner = next(n for n in result.nodes if n.node == "planner")
        assert planner.verdict == "mismatch"
        assert result.has_mismatch

    def test_missing_source_is_unverified_identity_not_inferred(self):
        manifest = _manifest()
        result = reconcile_attempt("a1", manifest, [_usage("planner", source=None)])
        planner = next(n for n in result.nodes if n.node == "planner")
        assert planner.verdict == "unverified_identity"
        assert result.has_unverified_identity

    def test_usage_for_node_absent_from_manifest_is_unmapped_call(self):
        manifest = _manifest()
        result = reconcile_attempt(
            "a1", manifest, [_usage("some_new_node_not_in_manifest", source="openai:gpt-5.6-sol")]
        )
        unmapped = next(n for n in result.nodes if n.node == "some_new_node_not_in_manifest")
        assert unmapped.verdict == "unmapped_call"
        assert result.has_unmapped_call

    def test_utils_node_usage_reconciles_as_match_not_unmapped_call(self):
        """outputter/hopper are declared in manifest["utils"], not manifest["nodes"].

        reconcile_attempt merges both dicts by node name before matching, so a
        real llm_usage event recorded under node="outputter" (e.g. the Flash
        runner's Outputter synthesis call, which fires on essentially every
        real run) must reconcile as "match" against the manifest's utils
        entry -- never as unmapped_call just because it lives under a
        different top-level manifest key than the twelve agent nodes.
        """
        manifest = _manifest()
        result = reconcile_attempt(
            "a1",
            manifest,
            [_usage("outputter"), _usage("hopper")],
        )
        outputter = next(n for n in result.nodes if n.node == "outputter")
        hopper = next(n for n in result.nodes if n.node == "hopper")
        assert outputter.verdict == "match"
        assert hopper.verdict == "match"
        assert not result.has_unmapped_call

    def test_enabled_node_never_invoked_is_not_invoked_informational(self):
        manifest = _manifest()
        result = reconcile_attempt("a1", manifest, [])
        planner = next(n for n in result.nodes if n.node == "planner")
        assert planner.verdict == "not_invoked"

    def test_disabled_node_never_invoked_is_disabled_not_invoked(self):
        manifest = _manifest()
        result = reconcile_attempt("a1", manifest, [])
        pixel_safety = next(n for n in result.nodes if n.node == "validator_pixel_safety_net")
        assert pixel_safety.verdict == "disabled_not_invoked"

    def test_never_fabricates_zero_usage_for_non_firing_node(self):
        manifest = _manifest()
        result = reconcile_attempt("a1", manifest, [_usage("planner")])
        # Every other enabled node with no usage event is not_invoked, never
        # synthesized as a zero-usage "match".
        others = [n for n in result.nodes if n.node != "planner" and n.manifest_enabled]
        assert others
        assert all(n.verdict == "not_invoked" for n in others)

    def test_aliased_trace_node_name_reconciles_against_manifest_node(self):
        """validator_pixel_safety_net is traced under "safety_net_pixel_validation"
        (see artemis.agents.validator.validator._validate_action_precondition_pixel's
        @trace name), so a usage event recorded with that trace-scope node
        name must reconcile as a match against the manifest's
        validator_pixel_safety_net entry, not surface as unmapped_call.
        """
        # Enable validator_pixel_safety_net explicitly so it has a real
        # manifest entry to reconcile against.
        config = _uniform_config()
        config = config.model_copy(update={"validator_pixel_safety_net": _llm()})
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a1",
            trace_id="t1",
            tier="sol",
            llm_config=config,
            env={},
            checkpoint="launch",
            now=1.0,
        )
        result = reconcile_attempt(
            "a1",
            manifest,
            [_usage("safety_net_pixel_validation", source="openai:gpt-5.6-sol")],
        )
        pixel_safety = next(n for n in result.nodes if n.node == "validator_pixel_safety_net")
        assert pixel_safety.verdict == "match"
        assert not result.has_unmapped_call
        # No stray "safety_net_pixel_validation" entry should remain unmapped.
        assert not any(n.node == "safety_net_pixel_validation" for n in result.nodes)

    def test_soft_defaulted_node_with_would_resolve_to_match_reconciles_as_match(self):
        """validator_pixel_safety_net left unset (the normal/default state) is
        soft-defaulted by LLMConfig.get_agent() to lightweight_judge_default()
        (google:gemini-3.5-flash-lite, a Luna-tier model) when actually
        invoked. The manifest entry is `enabled=False` (correctly recording it
        wasn't explicitly configured) but carries `would_resolve_to` for
        exactly this case. This is the *legitimate* positive case: a manifest
        whose own declared `tier` genuinely IS "luna" (i.e. its enabled nodes
        already resolve to Luna-tier models), so the soft default's tier
        agrees with the attempt's own tier. A receipt matching
        `would_resolve_to` here is a legitimate, expected production
        occurrence and must reconcile as "match", not "mismatch".
        """
        luna_config = _uniform_config(provider="google", model="gemini-3.5-flash-lite")
        manifest = _manifest(tier="luna", llm_config=luna_config)
        node_entry = manifest["nodes"]["validator_pixel_safety_net"]
        assert node_entry["enabled"] is False
        assert node_entry["would_resolve_to"] == {
            "provider": "google",
            "model": "gemini-3.5-flash-lite",
            "api_base": None,
            "temperature": 0.0,
            "thinking_budget": None,
            "thinking_level": None,
            "reasoning_effort": None,
            "include_thoughts": None,
            "enable_grounding": None,
            "fallback": {
                "provider": "google",
                "model": "gemini-3.1-flash-lite",
                "api_base": None,
                "temperature": 0.0,
                "thinking_budget": None,
                "thinking_level": None,
                "reasoning_effort": None,
                "include_thoughts": None,
                "enable_grounding": None,
            },
            "fix_model": None,
            "timeout": None,
        }

        result = reconcile_attempt(
            "a1",
            manifest,
            [_usage("safety_net_pixel_validation", source="google:gemini-3.5-flash-lite")],
        )
        pixel_safety = next(n for n in result.nodes if n.node == "validator_pixel_safety_net")
        assert pixel_safety.verdict == "match"
        # The manifest's own `enabled` field must stay honest: this node was
        # not explicitly configured, even though the receipt matched.
        assert pixel_safety.manifest_enabled is False

        record = _record(
            manifest, [_usage("safety_net_pixel_validation", source="google:gemini-3.5-flash-lite")]
        )
        verdict = validate_batch([record])
        assert verdict.accepted is True

    def test_soft_defaulted_node_cross_tier_would_resolve_to_is_rejected_not_matched(self):
        """Negative control for the cross-tier-mixing defect: a `tier="sol"`
        manifest's soft-defaulted validator_pixel_safety_net still would
        resolve to a Luna-tier model (google:gemini-3.5-flash-lite) if
        invoked, since would_resolve_to is a fixed function of the node, not
        of the attempt's own declared tier. A receipt matching that Luna-tier
        would_resolve_to must NOT reconcile as "match" against a Sol-pinned
        attempt -- that would let an attempt pinned to Sol tier silently
        invoke a Luna-tier model via the soft-default path. It must reconcile
        as "mismatch", and the batch must be rejected, not accepted.
        """
        manifest = _manifest(tier="sol")  # validator_pixel_safety_net left None (default).
        node_entry = manifest["nodes"]["validator_pixel_safety_net"]
        assert node_entry["enabled"] is False
        assert node_entry["would_resolve_to"]["provider"] == "google"
        assert node_entry["would_resolve_to"]["model"] == "gemini-3.5-flash-lite"

        result = reconcile_attempt(
            "a1",
            manifest,
            [_usage("safety_net_pixel_validation", source="google:gemini-3.5-flash-lite")],
        )
        pixel_safety = next(n for n in result.nodes if n.node == "validator_pixel_safety_net")
        assert pixel_safety.verdict == "mismatch"
        assert result.has_mismatch

        record = _record(
            manifest, [_usage("safety_net_pixel_validation", source="google:gemini-3.5-flash-lite")]
        )
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "unexpected_model_or_endpoint"

    def test_soft_defaulted_node_with_mismatched_source_still_reconciles_as_mismatch(self):
        """Negative control: this fix must not turn off legitimate mismatch
        detection for soft-defaulted nodes -- only make the *expected* source
        honest (derived from would_resolve_to instead of always None)."""
        manifest = _manifest()  # validator_pixel_safety_net left None (default).
        result = reconcile_attempt(
            "a1",
            manifest,
            [_usage("safety_net_pixel_validation", source="openai:gpt-5.6-sol")],
        )
        pixel_safety = next(n for n in result.nodes if n.node == "validator_pixel_safety_net")
        assert pixel_safety.verdict == "mismatch"
        assert result.has_mismatch


_UNSET = object()


def _record(manifest, usage_events, digest=_UNSET) -> AttemptRecord:
    reconciliation = reconcile_attempt(manifest["attempt_id"], manifest, usage_events)
    return AttemptRecord(
        attempt_id=manifest["attempt_id"],
        manifest=manifest,
        reconciliation=reconciliation,
        stored_digest=digest_of(manifest) if digest is _UNSET else digest,
    )


class TestBatchValidationAllSevenReasons:
    def test_empty_batch_is_missing_snapshot(self):
        verdict = validate_batch([])
        assert verdict.accepted is False
        assert verdict.reason == "missing_snapshot"

    def test_accept_when_batch_is_clean(self):
        manifest = _manifest()
        record = _record(manifest, [_usage("planner")])
        verdict = validate_batch([record])
        assert verdict.accepted is True
        assert verdict.reason is None
        assert verdict.invalid_attempts == ()

    def test_missing_identity(self):
        manifest = _manifest()
        broken = copy.deepcopy(manifest)
        broken["source_sha"] = ""
        record = _record(broken, [_usage("planner")], digest=digest_of(broken))
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "missing_identity"
        assert record in verdict.invalid_attempts

    def test_missing_identity_when_source_sha_is_unverified_sentinel(self):
        """A truthy but never-git-verified source_sha (the "0"*40 fallback
        _canonical_repo_sha emits when the tree isn't a real git checkout)
        must not silently pass as a real identity."""
        manifest = _manifest()
        unverified = copy.deepcopy(manifest)
        unverified["source_sha"] = "0" * 40
        unverified["source_sha_provenance"] = "unknown-not-a-git-checkout"
        record = _record(unverified, [_usage("planner")], digest=digest_of(unverified))
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "missing_identity"
        assert record in verdict.invalid_attempts

    def test_missing_identity_when_untiered_enabled_node_present(self):
        """An enabled node with no declared tier (custom/unknown-model) must
        fail batch validation, even though it built successfully and didn't
        trip the mixed_tier build-time check."""
        manifest = _manifest()
        untiered = copy.deepcopy(manifest)
        untiered["untiered_enabled_nodes"] = ["some_custom_node"]
        record = _record(untiered, [_usage("planner")], digest=digest_of(untiered))
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "missing_identity"
        assert record in verdict.invalid_attempts

    def test_fake_mode_enabled(self):
        manifest = _manifest(env={"ARTEMIS_FAKE_LLM": "1"})
        record = _record(manifest, [_usage("planner")])
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "fake_mode_enabled"

    def test_missing_snapshot_when_no_stored_digest(self):
        manifest = _manifest()
        record = _record(manifest, [_usage("planner")], digest=None)
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "missing_snapshot"

    def test_digest_drift_when_manifest_mutated_after_storing(self):
        manifest = _manifest()
        original_digest = digest_of(manifest)
        tampered = copy.deepcopy(manifest)
        tampered["nodes"]["planner"]["config"]["model"] = "some-other-model"
        record = _record(tampered, [_usage("planner")], digest=original_digest)
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "digest_drift"
        # The original (tampered) record is preserved for audit, not discarded.
        assert verdict.invalid_attempts[0].manifest["nodes"]["planner"]["config"]["model"] == (
            "some-other-model"
        )

    def test_mixed_tier_across_batch(self):
        sol_manifest = _manifest(tier="sol")
        luna_config = _uniform_config(provider="google", model="gemini-3.5-flash-lite")
        luna_manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a2",
            trace_id="t2",
            tier="luna",
            llm_config=luna_config,
            env={},
            checkpoint="launch",
            now=2.0,
        )
        records = [
            _record(sol_manifest, [_usage("planner")]),
            _record(luna_manifest, [_usage("planner", source="google:gemini-3.5-flash-lite")]),
        ]
        verdict = validate_batch(records)
        assert verdict.accepted is False
        assert verdict.reason == "mixed_tier"
        assert len(verdict.invalid_attempts) == 2

    def test_unmapped_call(self):
        manifest = _manifest()
        record = _record(manifest, [_usage("planner"), _usage("totally_unknown_node")])
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "unmapped_call"

    def test_unexpected_model_or_endpoint_from_mismatch(self):
        manifest = _manifest()
        record = _record(manifest, [_usage("planner", source="anthropic:claude-x")])
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "unexpected_model_or_endpoint"

    def test_unexpected_model_or_endpoint_from_unverified_identity(self):
        manifest = _manifest()
        record = _record(manifest, [_usage("planner", source=None)])
        verdict = validate_batch([record])
        assert verdict.accepted is False
        assert verdict.reason == "unexpected_model_or_endpoint"

    def test_all_seven_reject_reasons_are_reachable_and_documented(self):
        # Sanity check that the reasons enumerated in the spec are exactly
        # what REJECT_REASONS declares -- catches drift if a reason is
        # renamed in one place but not the other.
        assert set(REJECT_REASONS) == {
            "missing_identity",
            "unmapped_call",
            "mixed_tier",
            "unexpected_model_or_endpoint",
            "fake_mode_enabled",
            "digest_drift",
            "missing_snapshot",
        }

    def test_rejection_preserves_original_attempt_data_unmodified(self):
        manifest = _manifest(env={"ARTEMIS_FAKE_LLM": "1"})
        record = _record(manifest, [_usage("planner")])
        verdict = validate_batch([record])
        assert verdict.invalid_attempts[0].manifest is manifest
        assert verdict.invalid_attempts[0].reconciliation is record.reconciliation


@pytest.fixture
def _isolate_traces_dir(tmp_path, monkeypatch):
    """Point trace_store at an isolated tmp_path directory for this test only.

    Never touches the process's real TRACES_DIR or the real home directory.
    """
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))


def _seeded_db(tmp_path, session_id: str):
    db_path = tmp_path / "usage.db"
    traces_dir = tmp_path / "usage_traces"
    storage = StorageManager(db_path, traces_dir)
    storage.create_session(SessionMetadata(session_id=session_id, initial_goal="test goal"))
    return storage, db_path, traces_dir


class TestReconcileFinishedAttempt:
    """Post-run orchestration: stored manifest + native DB receipts -> verdict.

    Uses only tmp_path-backed trace storage and a tmp_path SQLite DB seeded
    via a writable StorageManager -- never the real traces directory or the
    real DataEngine database.
    """

    def test_no_stored_manifest_returns_none_not_a_verdict(self, tmp_path, _isolate_traces_dir):
        _storage, db_path, traces_dir = _seeded_db(tmp_path, "trace-no-manifest")
        result = reconcile_finished_attempt(
            trace_id="trace-no-manifest",
            session_id="trace-no-manifest",
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
        )
        assert result is None

    def test_matching_usage_accepts_the_attempt(self, tmp_path, _isolate_traces_dir):
        trace_id = "trace-accept"
        manifest = _manifest(attempt_id=trace_id, trace_id=trace_id)
        store_attempt_manifest(manifest)

        storage, db_path, traces_dir = _seeded_db(tmp_path, trace_id)
        for node in _REQUIRED_NODES:
            storage.create_trace(
                TraceRecord(
                    trace_id=f"usage-{node}",
                    session_id=trace_id,
                    type="llm_call",
                    name="llm_usage",
                    payload={"node": node, "source": "openai:gpt-5.6-sol"},
                )
            )

        verdict = reconcile_finished_attempt(
            trace_id=trace_id,
            session_id=trace_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
        )

        assert verdict is not None
        assert verdict.accepted is True

    def test_mismatched_usage_rejects_with_original_receipts_preserved(
        self, tmp_path, _isolate_traces_dir
    ):
        trace_id = "trace-reject"
        manifest = _manifest(attempt_id=trace_id, trace_id=trace_id)
        store_attempt_manifest(manifest)

        storage, db_path, traces_dir = _seeded_db(tmp_path, trace_id)
        # planner fires with a model that does not match the manifest's
        # pinned 'sol' identity -- a leaked/decoy config would look like this.
        storage.create_trace(
            TraceRecord(
                trace_id="usage-planner",
                session_id=trace_id,
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "google:gemini-3.5-flash-lite"},
            )
        )

        verdict = reconcile_finished_attempt(
            trace_id=trace_id,
            session_id=trace_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
        )

        assert verdict is not None
        assert verdict.accepted is False
        assert verdict.reason == "unexpected_model_or_endpoint"
        # The original mismatched receipt is retained on the reconciliation
        # result, not discarded because the batch was rejected.
        invalid = verdict.invalid_attempts[0]
        planner_node = next(n for n in invalid.reconciliation.nodes if n.node == "planner")
        assert planner_node.usage_sources == ("google:gemini-3.5-flash-lite",)

    def test_no_usage_events_at_all_is_not_invoked_not_a_fabricated_match(
        self, tmp_path, _isolate_traces_dir
    ):
        """No DB rows for this session (e.g. the task crashed before any LLM
        call) must reconcile every node as not_invoked/disabled_not_invoked,
        never a synthesized match."""
        trace_id = "trace-no-usage"
        manifest = _manifest(attempt_id=trace_id, trace_id=trace_id)
        store_attempt_manifest(manifest)
        _storage, db_path, traces_dir = _seeded_db(tmp_path, trace_id)

        verdict = reconcile_finished_attempt(
            trace_id=trace_id,
            session_id=trace_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
        )

        assert verdict is not None
        assert verdict.accepted is True


class TestReconcileAttemptBatchByRunId:
    """Cross-attempt batch reconciliation grouped by a shared run_id.

    This is the production-reachable home for the mixed_tier rejection path
    (reconcile_finished_attempt can never exercise it since it always
    validates a batch of exactly one attempt). Uses only tmp_path-backed
    trace storage and a tmp_path SQLite DB -- never the real traces
    directory or the real DataEngine database.
    """

    def test_two_attempts_same_run_id_same_tier_matching_usage_is_accepted(
        self, tmp_path, _isolate_traces_dir
    ):
        run_id = "run-accept"
        traces_root = Path(trace_store.TRACES_DIR)
        db_path = tmp_path / "usage.db"
        traces_dir = tmp_path / "usage_traces"
        storage = StorageManager(db_path, traces_dir)

        for trace_id in ("attempt-1", "attempt-2"):
            storage.create_session(SessionMetadata(session_id=trace_id, initial_goal="test goal"))
            manifest = _manifest(run_id=run_id, attempt_id=trace_id, trace_id=trace_id)
            store_attempt_manifest(manifest)
            for node in _REQUIRED_NODES:
                storage.create_trace(
                    TraceRecord(
                        trace_id=f"usage-{trace_id}-{node}",
                        session_id=trace_id,
                        type="llm_call",
                        name="llm_usage",
                        payload={"node": node, "source": "openai:gpt-5.6-sol"},
                    )
                )

        verdict = reconcile_attempt_batch_by_run_id(
            run_id=run_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
            traces_root=traces_root,
        )
        assert verdict.accepted is True
        assert verdict.invalid_attempts == ()

    def test_mixed_tier_across_attempts_sharing_run_id_is_rejected(
        self, tmp_path, _isolate_traces_dir
    ):
        run_id = "run-mixed-tier"
        traces_root = Path(trace_store.TRACES_DIR)
        db_path = tmp_path / "usage.db"
        traces_dir = tmp_path / "usage_traces"
        storage = StorageManager(db_path, traces_dir)

        sol_trace_id = "attempt-sol"
        storage.create_session(SessionMetadata(session_id=sol_trace_id, initial_goal="test goal"))
        sol_manifest = _manifest(run_id=run_id, attempt_id=sol_trace_id, trace_id=sol_trace_id)
        store_attempt_manifest(sol_manifest)
        storage.create_trace(
            TraceRecord(
                trace_id=f"usage-{sol_trace_id}-planner",
                session_id=sol_trace_id,
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "openai:gpt-5.6-sol"},
            )
        )

        terra_trace_id = "attempt-terra"
        storage.create_session(SessionMetadata(session_id=terra_trace_id, initial_goal="test goal"))
        terra_config = _uniform_config(provider="google", model="gemini-3.8-flash")
        terra_manifest = build_attempt_manifest(
            run_id=run_id,
            attempt_id=terra_trace_id,
            trace_id=terra_trace_id,
            tier="terra",
            llm_config=terra_config,
            env={},
            checkpoint="launch",
            now=3.0,
        )
        store_attempt_manifest(terra_manifest)
        storage.create_trace(
            TraceRecord(
                trace_id=f"usage-{terra_trace_id}-planner",
                session_id=terra_trace_id,
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "google:gemini-3.8-flash"},
            )
        )

        verdict = reconcile_attempt_batch_by_run_id(
            run_id=run_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
            traces_root=traces_root,
        )
        assert verdict.accepted is False
        assert verdict.reason == "mixed_tier"
        assert len(verdict.invalid_attempts) == 2
        invalid_ids = {a.attempt_id for a in verdict.invalid_attempts}
        assert invalid_ids == {sol_trace_id, terra_trace_id}

    def test_attempt_under_different_run_id_is_excluded_from_batch(
        self, tmp_path, _isolate_traces_dir
    ):
        run_id = "run-target"
        other_run_id = "run-other"
        traces_root = Path(trace_store.TRACES_DIR)
        db_path = tmp_path / "usage.db"
        traces_dir = tmp_path / "usage_traces"
        storage = StorageManager(db_path, traces_dir)

        target_trace_id = "attempt-target"
        storage.create_session(
            SessionMetadata(session_id=target_trace_id, initial_goal="test goal")
        )
        target_manifest = _manifest(
            run_id=run_id, attempt_id=target_trace_id, trace_id=target_trace_id
        )
        store_attempt_manifest(target_manifest)
        for node in _REQUIRED_NODES:
            storage.create_trace(
                TraceRecord(
                    trace_id=f"usage-{target_trace_id}-{node}",
                    session_id=target_trace_id,
                    type="llm_call",
                    name="llm_usage",
                    payload={"node": node, "source": "openai:gpt-5.6-sol"},
                )
            )

        # A different run_id, deliberately a different tier -- if this leaked
        # into the target batch it would falsely trip mixed_tier.
        other_trace_id = "attempt-other-run"
        storage.create_session(SessionMetadata(session_id=other_trace_id, initial_goal="test goal"))
        other_config = _uniform_config(provider="google", model="gemini-3.5-flash-lite")
        other_manifest = build_attempt_manifest(
            run_id=other_run_id,
            attempt_id=other_trace_id,
            trace_id=other_trace_id,
            tier="luna",
            llm_config=other_config,
            env={},
            checkpoint="launch",
            now=4.0,
        )
        store_attempt_manifest(other_manifest)
        storage.create_trace(
            TraceRecord(
                trace_id=f"usage-{other_trace_id}-planner",
                session_id=other_trace_id,
                type="llm_call",
                name="llm_usage",
                payload={"node": "planner", "source": "google:gemini-3.5-flash-lite"},
            )
        )

        verdict = reconcile_attempt_batch_by_run_id(
            run_id=run_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
            traces_root=traces_root,
        )
        assert verdict.accepted is True
        assert len(verdict.invalid_attempts) == 0

    def test_zero_matching_attempts_returns_missing_snapshot(self, tmp_path, _isolate_traces_dir):
        traces_root = Path(trace_store.TRACES_DIR)
        db_path = tmp_path / "usage.db"
        traces_dir = tmp_path / "usage_traces"

        verdict = reconcile_attempt_batch_by_run_id(
            run_id="run-does-not-exist",
            checkpoint="launch",
            db_path=db_path,
            traces_dir=traces_dir,
            traces_root=traces_root,
        )
        assert verdict.accepted is False
        assert verdict.reason == "missing_snapshot"
