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

"""Gate 1 mechanism tests: manifest determinism, storage, and the decoy-config
isolation control.

All tests use ``tmp_path``/``monkeypatch`` exclusively. None of them touch the
real home directory, the real ``~/.artemis``/PAL config dir, or make any
provider/network call.
"""

from __future__ import annotations

import json
import threading

import pytest

from artemis.config.attempt_manifest import (
    ManifestAlreadyExistsError,
    build_attempt_manifest,
    canonical_bytes,
    digest_of,
    read_stored_manifest,
    store_attempt_manifest,
)
from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback
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
    base = {node: _llm(provider, model) for node in _REQUIRED_NODES}
    return LLMConfig(
        utils=LLMConfigUtils(outputter=_llm(provider, model), hopper=_llm(provider, model)),
        **base,
    )


def _sol_config(**node_overrides: LLMWithFallback) -> LLMConfig:
    """A fully-populated LLMConfig where every required node resolves to 'sol'."""
    base = {node: _llm() for node in _REQUIRED_NODES}
    base.update(node_overrides)
    return LLMConfig(utils=LLMConfigUtils(outputter=_llm(), hopper=_llm()), **base)


@pytest.fixture(autouse=True)
def _isolate_traces_dir(tmp_path, monkeypatch):
    """Point trace_store at an isolated tmp_path directory for every test here.

    This is the concrete guard against ever touching a real traces directory:
    every test in this module writes manifests under ``tmp_path``, never under
    the process's real TRACES_DIR.
    """
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))


class TestCanonicalDigestStability:
    """Requirement 2: same logical manifest -> byte-identical canonical form."""

    def test_same_manifest_different_key_order_same_bytes_and_digest(self):
        config = _sol_config()
        manifest_a = build_attempt_manifest(
            run_id="run-1",
            attempt_id="attempt-1",
            trace_id="trace-1",
            tier="sol",
            llm_config=config,
            env={"ARTEMIS_CONFIG_DIR": "/tmp/does-not-matter"},
            checkpoint="launch",
            now=1000.0,
        )

        # Same logical content, deliberately constructed via a different
        # insertion order (dict equality ignores order, but a naive
        # json.dumps without sort_keys would NOT produce identical bytes).
        reordered = {k: manifest_a[k] for k in reversed(list(manifest_a.keys()))}
        reordered["nodes"] = {
            k: manifest_a["nodes"][k] for k in reversed(list(manifest_a["nodes"].keys()))
        }

        assert reordered == manifest_a
        assert canonical_bytes(reordered) == canonical_bytes(manifest_a)
        assert digest_of(reordered) == digest_of(manifest_a)

    def test_canonical_bytes_have_no_whitespace_ambiguity(self):
        manifest = build_attempt_manifest(
            run_id="run-1",
            attempt_id="attempt-1",
            trace_id="trace-1",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=1000.0,
        )
        raw = canonical_bytes(manifest)
        # No ambiguous JSON *structural* whitespace: a naive json.dumps(...,
        # indent=2) would introduce ", " item separators and ": " key
        # separators immediately followed by a structural character; our
        # canonical form uses bare "," and ":" everywhere a separator is
        # structurally required. Prose values (e.g. "no soft default: ...")
        # legitimately contain ": " as English punctuation, so assert on the
        # compact-separator round trip instead of a raw substring search.
        assert (
            json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            == raw
        )
        assert b"\n" not in raw
        assert b"  " not in raw  # no accidental double-space from indent=2-style output
        # Round-trips to the same logical object.
        assert json.loads(raw.decode("utf-8")) == manifest

    def test_digest_changes_when_a_non_tier_field_changes(self):
        config_a = _sol_config()
        # temperature is not part of the tier match (TIER_MODELS only looks
        # at provider/model), so this stays within tier 'sol' while still
        # changing a field that must affect the digest.
        config_b = _sol_config(
            planner=_llm(model="gpt-5.6-sol").model_copy(update={"temperature": 0.9})
        )
        manifest_a = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=config_a,
            env={},
            checkpoint="launch",
            now=1.0,
        )
        manifest_b = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=config_b,
            env={},
            checkpoint="launch",
            now=1.0,
        )
        assert digest_of(manifest_a) != digest_of(manifest_b)

    def test_mixed_tier_within_one_manifest_is_rejected_at_build_time(self):
        config = _sol_config(planner=_llm(provider="google", model="gemini-3.5-flash-lite"))
        with pytest.raises(ValueError, match="mixed_tier|other tier"):
            build_attempt_manifest(
                run_id="r",
                attempt_id="a",
                trace_id="t",
                tier="sol",
                llm_config=config,
                env={},
                checkpoint="launch",
                now=1.0,
            )


class TestAnthropicTierResolution:
    """CHE-639: the 'opus' tier maps to anthropic/claude-sonnet-5."""

    def test_anthropic_claude_sonnet_5_config_resolves_to_opus_tier(self):
        config = _uniform_config(provider="anthropic", model="claude-sonnet-5")
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="opus",
            llm_config=config,
            env={},
            checkpoint="launch",
            now=1.0,
        )
        planner_entry = manifest["nodes"]["planner"]
        assert planner_entry["resolved_tier"] == "opus"
        assert manifest["tier"] == "opus"
        assert manifest["untiered_enabled_nodes"] == []

    def test_unmapped_anthropic_model_is_untiered_not_silently_opus(self):
        # claude-sonnet-4-5 is not in TIER_MODELS (only claude-sonnet-5 is
        # mapped, for the 'opus' tier) -- every enabled node must resolve to
        # no tier at all, not silently fall back to 'opus'. Declaring
        # tier="opus" here without any node actually matching it must not
        # raise mixed_tier (no *other* declared tier is present either), but
        # every such node lands in untiered_enabled_nodes.
        config = _uniform_config(provider="anthropic", model="claude-sonnet-4-5")
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="opus",
            llm_config=config,
            env={},
            checkpoint="launch",
            now=1.0,
        )
        assert manifest["nodes"]["planner"]["resolved_tier"] is None
        assert "planner" in manifest["untiered_enabled_nodes"]

    def test_record_attempt_manifest_hook_infers_opus_tier_from_planner(
        self, tmp_path, monkeypatch
    ):
        # The real skip/store decision lives in attempt_lifecycle_hooks, which
        # infers the tier from llm_config.planner rather than taking a tier
        # argument -- this is the exact loop that emitted "does not match a
        # declared tier" for anthropic/claude-sonnet-4-5 during the CHE-491
        # pilot. Assert it now resolves for the mapped model.
        from artemis.config.attempt_lifecycle_hooks import record_attempt_manifest

        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        config = _uniform_config(provider="anthropic", model="claude-sonnet-5")
        record_attempt_manifest(trace_id="hook-trace-1", checkpoint="launch", llm_config=config)
        stored_bytes, _digest = read_stored_manifest("hook-trace-1", "launch")
        assert json.loads(stored_bytes)["tier"] == "opus"


class TestNullableNodesRepresentedExplicitly:
    def test_disabled_nodes_show_would_resolve_default_without_being_active(self):
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=1.0,
        )
        pixel_safety = manifest["nodes"]["validator_pixel_safety_net"]
        assert pixel_safety["enabled"] is False
        assert pixel_safety["config"] is None
        assert pixel_safety["would_resolve_to"]["provider"] == "google"
        assert pixel_safety["would_resolve_to"]["model"] == "gemini-3.5-flash-lite"

        history_analyzer = manifest["nodes"]["history_analyzer"]
        assert history_analyzer["enabled"] is False
        assert history_analyzer["would_resolve_provenance"] == "soft-default: inherits 'operator'"

        video_analyzer = manifest["utils"]["video_analyzer"]
        assert video_analyzer["enabled"] is False
        assert video_analyzer["would_resolve_to"] is None
        assert "no soft default" in video_analyzer["would_resolve_provenance"]

    def test_fake_llm_flag_and_credentials_never_leak_secrets(self):
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=_sol_config(),
            env={"OPENAI_API_KEY": "sk-supersecretvalue1234", "ARTEMIS_FAKE_LLM": "0"},
            checkpoint="launch",
            now=1.0,
        )
        assert manifest["fake_llm_enabled"] is False
        raw = canonical_bytes(manifest)
        assert b"sk-supersecretvalue1234" not in raw
        assert manifest["credentials"]["openai"]["set"] is True
        assert manifest["credentials"]["openai"]["last4"] == "1234"

    def test_credential_shaped_artemis_env_vars_never_leak_raw_value(self):
        """ARTEMIS_TENANT_TOKEN (a real secret-bearing var, see
        artemis.config.constants.ENV_ARTEMIS_TENANT_TOKEN) must never appear
        by raw value in env_overrides or the canonical bytes -- only a
        set/last4 presence reference, exactly like _credential_reference.
        """
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=_sol_config(),
            env={
                "ARTEMIS_TENANT_TOKEN": "super-secret-tenant-token-value",
                "ARTEMIS_LIFECYCLE_TOKEN": "another-secret-lifecycle-value",
                "ARTEMIS_FAKE_LLM": "0",
                "ARTEMIS_CONFIG_DIR": "/tmp/does-not-matter",
            },
            checkpoint="launch",
            now=1.0,
        )
        raw = canonical_bytes(manifest)
        assert b"super-secret-tenant-token-value" not in raw
        assert b"another-secret-lifecycle-value" not in raw

        tenant_ref = manifest["env_overrides"]["ARTEMIS_TENANT_TOKEN"]
        assert tenant_ref == {"set": True, "last4": "alue"}
        lifecycle_ref = manifest["env_overrides"]["ARTEMIS_LIFECYCLE_TOKEN"]
        assert lifecycle_ref == {"set": True, "last4": "alue"}

        # Non-sensitive ARTEMIS_* resolution knobs still pass through as raw
        # values -- this fix must not overcorrect into hashing everything.
        assert manifest["env_overrides"]["ARTEMIS_CONFIG_DIR"] == "/tmp/does-not-matter"
        assert manifest["env_overrides"]["ARTEMIS_FAKE_LLM"] == "0"

    def test_unset_credential_shaped_env_var_reports_set_false_without_last4(self):
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="t",
            tier="sol",
            llm_config=_sol_config(),
            env={"ARTEMIS_TENANT_TOKEN": ""},
            checkpoint="launch",
            now=1.0,
        )
        assert manifest["env_overrides"]["ARTEMIS_TENANT_TOKEN"] == {"set": False}


class TestStorageCreateOnly:
    def test_store_and_read_back_bytes_match(self, tmp_path):
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="trace-store-1",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=1.0,
        )
        path, digest = store_attempt_manifest(manifest)
        assert path.exists()
        assert digest == digest_of(manifest)

        stored_bytes, stored_digest = read_stored_manifest("trace-store-1", "launch")
        assert stored_bytes == canonical_bytes(manifest)
        assert stored_digest == digest

    def test_second_write_for_same_trace_and_checkpoint_is_rejected(self):
        manifest = build_attempt_manifest(
            run_id="r",
            attempt_id="a",
            trace_id="trace-store-2",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=1.0,
        )
        store_attempt_manifest(manifest)
        with pytest.raises(ManifestAlreadyExistsError):
            store_attempt_manifest(manifest)

    def test_concurrent_writers_never_overwrite_each_others_bytes(self):
        """Reproduces the TOCTOU this storage must not have.

        A prior implementation checked ``Path.exists()`` then wrote via
        ``os.replace()`` — a second writer landing between those two steps
        silently clobbered the first writer's already-stored bytes instead of
        raising. This drives two logically different manifests (same
        trace/checkpoint key, different ``run_id``) through
        ``store_attempt_manifest`` back-to-back and asserts the file on disk
        still holds exactly the first writer's bytes, with the second writer
        observing ``ManifestAlreadyExistsError`` rather than winning a race.
        """
        first = build_attempt_manifest(
            run_id="first-writer",
            attempt_id="a",
            trace_id="trace-race-1",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=1.0,
        )
        second = build_attempt_manifest(
            run_id="second-writer",
            attempt_id="a",
            trace_id="trace-race-1",
            tier="sol",
            llm_config=_sol_config(),
            env={},
            checkpoint="launch",
            now=2.0,
        )
        assert canonical_bytes(first) != canonical_bytes(second)

        first_path, first_digest = store_attempt_manifest(first)
        with pytest.raises(ManifestAlreadyExistsError):
            store_attempt_manifest(second)

        stored_bytes, stored_digest = read_stored_manifest("trace-race-1", "launch")
        assert stored_bytes == canonical_bytes(first)
        assert stored_digest == first_digest
        assert first_path.read_bytes() == canonical_bytes(first)

    def test_two_threads_racing_the_same_key_leave_exactly_one_winner(self):
        """True concurrent reproduction: two threads, one (trace_id, checkpoint).

        Exactly one thread's bytes must land on disk and the other must
        observe ``ManifestAlreadyExistsError`` — never a torn write and never
        a silent overwrite of the winner by the loser.
        """
        manifest_by_run_id = {
            run_id: build_attempt_manifest(
                run_id=run_id,
                attempt_id="a",
                trace_id="trace-race-2",
                tier="sol",
                llm_config=_sol_config(),
                env={},
                checkpoint="launch",
                now=float(i),
            )
            for i, run_id in enumerate(("racer-a", "racer-b"))
        }
        results: dict[str, tuple[str, str] | Exception] = {}
        start = threading.Barrier(2)

        def _race(run_id: str) -> None:
            start.wait()
            try:
                results[run_id] = store_attempt_manifest(manifest_by_run_id[run_id])
            except ManifestAlreadyExistsError as exc:
                results[run_id] = exc

        threads = [threading.Thread(target=_race, args=(rid,)) for rid in manifest_by_run_id]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [rid for rid, res in results.items() if not isinstance(res, Exception)]
        losers = [rid for rid, res in results.items() if isinstance(res, Exception)]
        assert len(winners) == 1
        assert len(losers) == 1
        assert isinstance(results[losers[0]], ManifestAlreadyExistsError)

        winning_manifest = manifest_by_run_id[winners[0]]
        stored_bytes, stored_digest = read_stored_manifest("trace-race-2", "launch")
        assert stored_bytes == canonical_bytes(winning_manifest)
        assert stored_digest == digest_of(winning_manifest)

    def test_different_checkpoints_for_same_trace_coexist(self):
        base_kwargs = dict(
            run_id="r",
            attempt_id="a",
            trace_id="trace-store-3",
            tier="sol",
            llm_config=_sol_config(),
            env={},
        )
        launch = build_attempt_manifest(**base_kwargs, checkpoint="launch", now=1.0)
        worker_start = build_attempt_manifest(**base_kwargs, checkpoint="worker_start", now=2.0)
        termination = build_attempt_manifest(**base_kwargs, checkpoint="termination", now=3.0)

        store_attempt_manifest(launch)
        store_attempt_manifest(worker_start)
        store_attempt_manifest(termination)

        for checkpoint in ("launch", "worker_start", "termination"):
            assert read_stored_manifest("trace-store-3", checkpoint) is not None


class TestDecoyConfigIsolation:
    """Requirement 3(b): decoy higher-priority config file must not leak
    across independently-resolved attempts, and a stored manifest must never
    change after the fact.

    This exercises the *env-var-driven config resolution layer in-process*:
    ``build_attempt_manifest`` never reads ``os.environ`` itself, so the
    "decoy" here is simulated purely via the explicit ``env`` dict and an
    isolated ``tmp_path`` config directory -- the real ``get_config_path()``
    cascade, the real user home directory, and the real MCP/device process
    boundary are NOT exercised by this test (see the PR description / final
    report for the explicit isolation-boundary callout).
    """

    def test_attempt_a_manifest_and_digest_survive_later_decoy_mutation(self, tmp_path):
        decoy_dir = tmp_path / "decoy-config-dir"
        decoy_dir.mkdir()
        decoy_override = decoy_dir / "llm-config.override.jsonc"
        decoy_override.write_text('{"nodes": {}}', encoding="utf-8")

        # Attempt A resolves using an explicit env snapshot that "points at"
        # the decoy dir (simulating ARTEMIS_CONFIG_DIR) plus a config object
        # already fully resolved for tier 'sol'. Nothing here reads the decoy
        # file's contents -- build_attempt_manifest only ever consumes
        # explicit llm_config/env/tier, which is exactly the property under
        # test: an env var *naming* a decoy path cannot retroactively alter a
        # manifest that was built from a different, already-resolved config.
        env_pointing_at_decoy = {"ARTEMIS_CONFIG_DIR": str(decoy_dir)}
        manifest_a = build_attempt_manifest(
            run_id="run-iso",
            attempt_id="attempt-a",
            trace_id="trace-iso-a",
            tier="sol",
            llm_config=_sol_config(),
            env=env_pointing_at_decoy,
            checkpoint="launch",
            now=10.0,
        )
        path_a, digest_a = store_attempt_manifest(manifest_a)
        bytes_a_before = path_a.read_bytes()

        # Mutate the decoy file to something that would resolve to a
        # different (mixed) tier if it were ever loaded and passed in.
        decoy_override.write_text(
            '{"nodes": {"planner": {"provider": "anthropic", "model": "claude-x"}}}',
            encoding="utf-8",
        )

        # Attempt B resolves independently -- its own explicit llm_config,
        # built fresh, deliberately picking a different tier to prove
        # resolution is not sticky/global.
        manifest_b = build_attempt_manifest(
            run_id="run-iso",
            attempt_id="attempt-b",
            trace_id="trace-iso-b",
            tier="luna",
            llm_config=_uniform_config(provider="google", model="gemini-3.5-flash-lite"),
            env={"ARTEMIS_CONFIG_DIR": str(decoy_dir)},
            checkpoint="launch",
            now=20.0,
        )
        store_attempt_manifest(manifest_b)

        # Attempt A's already-stored manifest is untouched, byte-for-byte.
        bytes_a_after = path_a.read_bytes()
        assert bytes_a_after == bytes_a_before
        _, digest_a_reread = read_stored_manifest("trace-iso-a", "launch")
        assert digest_a_reread == digest_a

        # Attempt B resolved to its own distinct tier/digest -- the decoy
        # mutation influenced neither attempt (A never re-read it; B never
        # read it at all, since resolution is a pure function of the
        # explicit llm_config argument).
        assert manifest_a["tier"] == "sol"
        assert manifest_b["tier"] == "luna"
        assert digest_of(manifest_a) != digest_of(manifest_b)

        # Never touched a real home directory or the real PAL config dir.
        assert str(decoy_dir).startswith(str(tmp_path))
