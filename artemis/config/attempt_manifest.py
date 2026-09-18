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

"""Per-attempt, credential-free LLM identity manifest (Gate 1 mechanism).

Builds one complete, provable record of exactly which model/provider/tier
resolved for every model-bearing node before an attempt starts inference,
serializes it to a deterministic canonical byte form, hashes it, and writes
it create-only beside the attempt's trace directory
(:func:`artemis.runtime.trace_store.get_trace_dir`).

This module implements the *mechanism* only. It does not itself run a
qualification pilot, and nothing here should be read as evidence that any
Gate 1 qualification has passed — see ``artemis.config.attempt_reconciliation``
for the usage-vs-manifest reconciliation and batch accept/reject rule that
consume this manifest's output.

Isolation scope note: :func:`build_attempt_manifest` is a pure function of
its explicit arguments (an ``LLMConfig``, a tier label, and an explicit env
snapshot dict) plus process-global constants it does not mutate. It does not
read ``os.environ`` directly and does not touch ``ModelFactory``'s
process-wide model cache. That makes it possible to prove, in-process, that
two calls with different explicit inputs never share state — see the unit
tests for the specific isolation boundary this actually exercises versus the
real MCP/device process boundary, which this module does not touch.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from artemis.config.constants import AgentNode, LLMUtilsNode
from artemis.config.llm import LLM, LLMConfig, LLMWithFallback, lightweight_judge_default
from artemis.runtime import trace_store

# ==============================================================================
# Tier mapping
# ==============================================================================

#: Internal model-tier labels (Luna/Terra/Sol cheap/mid/premium) used across
#: Cheese Work squads. Every enabled model-bearing node in a manifest must
#: resolve to exactly one of these tiers (mixed tiers => ``mixed_tier``
#: rejection, see ``attempt_reconciliation.validate_batch``).
Tier = Literal["luna", "terra", "sol"]

TIERS: tuple[Tier, ...] = ("luna", "terra", "sol")

# Provider/model pairs that identify each tier. This fork's config/artemis.jsonc
# does not (yet) declare an explicit tier->model table; its "default" node
# resolves to openai/gpt-5.6-sol today, so "sol" is mapped to that pair as the
# only tier presently exercised end-to-end. "luna"/"terra" are declared for
# forward compatibility with the escalation controller (Gate 2, out of scope
# here) and must be updated here (not inferred) if/when config/artemis.jsonc
# grows an explicit tier table.
TIER_MODELS: dict[Tier, tuple[str, str]] = {
    "luna": ("google", "gemini-3.5-flash-lite"),
    "terra": ("google", "gemini-3.8-flash"),
    "sol": ("openai", "gpt-5.6-sol"),
}

# Environment variables whose presence/value can influence LLMConfig
# resolution and must be captured (never their raw secret value) in the
# manifest's provenance section.
_RELEVANT_ENV_PREFIXES = ("ARTEMIS_",)
_FAKE_LLM_ENV_VAR = "ARTEMIS_FAKE_LLM"

# Substrings that mark an ARTEMIS_* variable name as credential-shaped (e.g.
# ARTEMIS_TENANT_TOKEN, ARTEMIS_LIFECYCLE_TOKEN). Matched case-insensitively
# against the whole var name. Any ARTEMIS_* var matching one of these must
# never have its raw value copied into the manifest's env_overrides -- it is
# reported the same non-secret way as _credential_reference (set + last4)
# instead. This is deliberately a denylist of credential-shaped substrings,
# not an allowlist of every non-secret ARTEMIS_* knob, since new resolution
# knobs are added far more often than new credential-bearing vars.
_CREDENTIAL_SHAPED_NAME_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
)


def _is_credential_shaped_env_name(name: str) -> bool:
    """True if ``name`` looks like it carries a secret (by name only)."""
    upper = name.upper()
    return any(marker in upper for marker in _CREDENTIAL_SHAPED_NAME_MARKERS)


def _sensitive_env_reference(value: str) -> dict[str, Any]:
    """Non-secret presence reference for one credential-shaped env var value.

    Mirrors :func:`_credential_reference`'s shape exactly: never includes the
    raw value, and omits ``last4`` entirely when the value is empty or
    shorter than 4 characters.
    """
    ref: dict[str, Any] = {"set": bool(value)}
    if value and len(value) >= 4:
        ref["last4"] = value[-4:]
    return ref


# Every LLMConfig field that can carry an LLMWithFallback, in the exact
# vocabulary of the spec. Fields not present in LLMConfig.model_fields are a
# programming error (caught by the assertion in build_attempt_manifest).
_TOP_LEVEL_NODES: tuple[AgentNode, ...] = (
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
    "history_analyzer",
    "validator_pixel_safety_net",
    "planner_validation",
    "output_analyzer",
)

# Nodes that are soft-defaulted by LLMConfig.get_agent() when absent, and the
# node they inherit from / default factory they use. Mirrors the branches in
# LLMConfig.get_agent verbatim so the manifest's "would resolve to" values
# never drift from the real resolution code.
_SOFT_DEFAULT_INHERITS_FROM: dict[str, str] = {
    "history_analyzer": "operator",
    "output_analyzer": "log_analyzer",
}
_SOFT_DEFAULT_FACTORY_NODES: frozenset[str] = frozenset(
    {"validator_pixel_safety_net", "planner_validation"}
)

_UTILS_NODES: tuple[LLMUtilsNode, ...] = (
    "outputter",
    "hopper",
    "video_analyzer",
    "object_detector",
)

# Nullable utils nodes have no soft-default factory (LLMConfig.get_utils raises
# instead); the manifest still records what they would need to be configured
# as, without pretending they are active.
_NULLABLE_UTILS_NODES: frozenset[str] = frozenset({"video_analyzer", "object_detector"})


def _canonical_repo_sha(repo_root: Path | None) -> tuple[str, str]:
    """Returns ``(sha, source)`` for the artemis repo's current commit.

    ``source`` is ``"git"`` on success. On any failure (not a git checkout,
    git missing, detached worktree issue, etc.) falls back to
    ``"unknown"`` with a fixed sentinel SHA rather than raising — a Gate 1
    manifest must always build, even in a source tree that lost its .git
    directory (e.g. an extracted archive or bundled wheel), but the fallback
    must be unambiguous in the manifest, never mistaken for a real SHA.
    """
    root = repo_root or Path(__file__).resolve().parent.parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        sha = result.stdout.strip()
        if sha:
            return sha, "git"
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40, "unknown-not-a-git-checkout"


def _relevant_env_snapshot(env: dict[str, str]) -> dict[str, Any]:
    """Filters an explicit env mapping down to Artemis-relevant keys only.

    Never includes raw credential values. Most ``ARTEMIS_*`` vars are
    resolution knobs (e.g. ``ARTEMIS_FAKE_LLM``, ``ARTEMIS_CONFIG_DIR``) and
    pass through as-is. Any ``ARTEMIS_*`` var whose *name* is credential-shaped
    (see ``_CREDENTIAL_SHAPED_NAME_MARKERS`` -- e.g. ``ARTEMIS_TENANT_TOKEN``)
    is never copied by raw value: it is replaced with the same non-secret
    ``{"set": bool, "last4": ...}`` shape ``_credential_reference`` already
    uses for provider API keys, so a manifest can never leak a raw
    credential into ``env_overrides`` even though provider credentials are
    also reported separately in the ``credentials`` section.
    """
    out: dict[str, Any] = {}
    for k, v in env.items():
        if not k.startswith(_RELEVANT_ENV_PREFIXES):
            continue
        if _is_credential_shaped_env_name(k):
            out[k] = _sensitive_env_reference(v)
        else:
            out[k] = v
    return out


def _credential_reference(env: dict[str, str], provider: str) -> dict[str, Any]:
    """Non-secret credential presence reference for one provider: set + last-4.

    Never includes the raw key. ``last4`` is omitted entirely when the key is
    unset or shorter than 4 characters, rather than emitting an empty string.
    """
    env_var = {
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "vertexai": None,
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPEN_ROUTER_API_KEY",
        "xai": "XAI_API_KEY",
        "ollama": None,
        "vllm": None,
        "custom": None,
    }.get(provider)
    if env_var is None:
        return {"provider": provider, "credential_env_var": None, "set": None}
    value = env.get(env_var)
    ref: dict[str, Any] = {
        "provider": provider,
        "credential_env_var": env_var,
        "set": bool(value),
    }
    if value and len(value) >= 4:
        ref["last4"] = value[-4:]
    return ref


def _llm_node_snapshot(llm: LLM | LLMWithFallback) -> dict[str, Any]:
    """Sanitized, non-secret snapshot of one LLM/LLMWithFallback record."""
    snap: dict[str, Any] = {
        "provider": llm.provider,
        "model": llm.model,
        "api_base": llm.api_base,
        "temperature": llm.temperature,
        "thinking_budget": llm.thinking_budget,
        "thinking_level": llm.thinking_level,
        "reasoning_effort": llm.reasoning_effort,
        "include_thoughts": llm.include_thoughts,
        "enable_grounding": llm.enable_grounding,
    }
    if isinstance(llm, LLMWithFallback):
        snap["fallback"] = _llm_node_snapshot(llm.fallback)
        snap["fix_model"] = llm.fix_model
        snap["timeout"] = llm.timeout
    return snap


def _node_tier(llm: LLM | LLMWithFallback) -> Tier | None:
    """Which declared tier (if any) this node's primary provider/model matches."""
    for tier, (provider, model) in TIER_MODELS.items():
        if llm.provider == provider and llm.model == model:
            return tier
    return None


@dataclasses.dataclass(frozen=True)
class NodeManifestEntry:
    """One node's manifest entry: enabled/disabled state, snapshot, and tier match."""

    node: str
    enabled: bool
    provenance: str
    config: dict[str, Any] | None
    resolved_tier: Tier | None
    # For disabled (None-valued, soft-defaulted) nodes only: what get_agent()
    # would actually return if this node were invoked right now, so the
    # manifest never silently omits the effective default.
    would_resolve_to: dict[str, Any] | None = None
    would_resolve_provenance: str | None = None


def _build_node_entry(
    node: str,
    raw_value: LLMWithFallback | None,
    config: LLMConfig,
    layer_hint: str,
) -> NodeManifestEntry:
    if raw_value is not None:
        snapshot = _llm_node_snapshot(raw_value)
        return NodeManifestEntry(
            node=node,
            enabled=True,
            provenance=layer_hint,
            config=snapshot,
            resolved_tier=_node_tier(raw_value),
        )

    # Explicitly disabled/nullable node. Represent it as disabled, and also
    # show what get_agent() would resolve it to if invoked, without treating
    # that hypothetical resolution as an active manifest entry.
    would_resolve_to: dict[str, Any] | None = None
    would_resolve_provenance: str | None = None
    if node in _SOFT_DEFAULT_INHERITS_FROM:
        inherited_from = _SOFT_DEFAULT_INHERITS_FROM[node]
        inherited_value = getattr(config, inherited_from)
        would_resolve_to = _llm_node_snapshot(inherited_value)
        would_resolve_provenance = f"soft-default: inherits '{inherited_from}'"
    elif node in _SOFT_DEFAULT_FACTORY_NODES:
        would_resolve_to = _llm_node_snapshot(lightweight_judge_default())
        would_resolve_provenance = "soft-default: lightweight_judge_default()"
    elif node in _NULLABLE_UTILS_NODES:
        would_resolve_provenance = "no soft default: get_utils() raises if invoked while unset"

    return NodeManifestEntry(
        node=node,
        enabled=False,
        provenance="disabled (None in LLMConfig)",
        config=None,
        resolved_tier=None,
        would_resolve_to=would_resolve_to,
        would_resolve_provenance=would_resolve_provenance,
    )


def _node_entries_to_dict(entries: list[NodeManifestEntry]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        out[entry.node] = {
            "enabled": entry.enabled,
            "provenance": entry.provenance,
            "config": entry.config,
            "resolved_tier": entry.resolved_tier,
            "would_resolve_to": entry.would_resolve_to,
            "would_resolve_provenance": entry.would_resolve_provenance,
        }
    return out


def build_attempt_manifest(
    *,
    run_id: str,
    attempt_id: str,
    trace_id: str,
    tier: Tier,
    llm_config: LLMConfig,
    env: dict[str, str],
    checkpoint: Literal["launch", "worker_start", "termination"],
    parent_attempt_id: str | None = None,
    config_layer_hint: str = "resolved LLMConfig (layer not separately tracked by caller)",
    repo_root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Builds one complete, credential-free attempt manifest.

    Pure function of its explicit arguments: it never reads ``os.environ``
    itself (the caller must snapshot it into ``env``) and never touches
    ``ModelFactory``'s process-wide model cache. Two calls with different
    ``env``/``llm_config``/``tier`` inputs are guaranteed to diverge; two
    calls with identical inputs (module state held constant) are guaranteed
    to produce logically identical manifests, modulo ``now``/checkpoint.

    ``config_layer_hint`` documents which resolution layer produced
    ``llm_config`` as a whole (e.g. "env override: ARTEMIS_CONFIG_DIR",
    "config/artemis.jsonc", "hardcoded default in parse_llm_config"); callers
    that track richer per-field provenance may pass a more specific string,
    or post-process the returned dict's per-node ``provenance`` values.

    Raises ``ValueError`` if ``tier`` is not one of the declared tiers, or if
    the resulting manifest mixes tiers across enabled nodes (fail fast at
    build time rather than deferring the mixed_tier check to batch
    validation, since a single manifest that already disagrees with itself
    should never be persisted as if it were valid).
    """
    if tier not in TIER_MODELS:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {TIERS}")

    sha, sha_source = _canonical_repo_sha(repo_root)
    relevant_env = _relevant_env_snapshot(env)
    fake_llm_value = env.get(_FAKE_LLM_ENV_VAR)
    fake_llm_enabled = fake_llm_value == "1"

    node_entries = [
        _build_node_entry(node, getattr(llm_config, node), llm_config, config_layer_hint)
        for node in _TOP_LEVEL_NODES
    ]
    util_entries = [
        _build_node_entry(util, getattr(llm_config.utils, util), llm_config, config_layer_hint)
        for util in _UTILS_NODES
    ]

    enabled_tiers = {
        e.resolved_tier for e in (*node_entries, *util_entries) if e.enabled and e.resolved_tier
    }
    untiered_enabled = [
        e.node for e in (*node_entries, *util_entries) if e.enabled and e.resolved_tier is None
    ]
    if enabled_tiers - {tier}:
        raise ValueError(
            f"Attempt manifest requested tier {tier!r} but enabled node(s) resolve to "
            f"other tier(s) {sorted(enabled_tiers - {tier})}; refusing to build a "
            "self-contradicting manifest. This is the mixed_tier condition."
        )

    credentials = {
        provider: _credential_reference(env, provider)
        for provider in ("openai", "google", "anthropic", "openrouter", "xai")
    }

    manifest: dict[str, Any] = {
        "manifest_schema_version": 1,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "trace_id": trace_id,
        "parent_attempt_id": parent_attempt_id,
        "checkpoint": checkpoint,
        "recorded_at": now if now is not None else time.time(),
        "tier": tier,
        "source_sha": sha,
        "source_sha_provenance": sha_source,
        "fake_llm_enabled": fake_llm_enabled,
        "fake_llm_env_var": _FAKE_LLM_ENV_VAR,
        "env_overrides": relevant_env,
        "credentials": credentials,
        "nodes": _node_entries_to_dict(node_entries),
        "utils": _node_entries_to_dict(util_entries),
        "untiered_enabled_nodes": sorted(untiered_enabled),
    }
    return manifest


# ==============================================================================
# Canonical serialization + hashing
# ==============================================================================


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    """Deterministic UTF-8 canonical JSON: sorted keys, no whitespace ambiguity.

    Uses ``json.dumps(..., sort_keys=True, ensure_ascii=False,
    separators=(",", ":"))``. Two dicts that are equal (regardless of key
    insertion order, since Python dict equality ignores order) always
    serialize to byte-identical output under this scheme.
    """
    return json.dumps(
        manifest,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_of(manifest: dict[str, Any]) -> str:
    """SHA-256 hex digest over ``canonical_bytes(manifest)``."""
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


# ==============================================================================
# Create-only storage beside the trace directory
# ==============================================================================


class ManifestAlreadyExistsError(FileExistsError):
    """Raised when an attempt manifest write would overwrite an existing file."""


def _manifest_paths(trace_id: str, checkpoint: str) -> tuple[Path, Path]:
    trace_dir = Path(trace_store.get_trace_dir(trace_id))
    manifest_path = trace_dir / f"attempt_manifest.{checkpoint}.json"
    digest_path = trace_dir / f"attempt_manifest.{checkpoint}.sha256"
    return manifest_path, digest_path


def _create_only_write(path: Path, data: bytes) -> None:
    """Writes ``data`` to ``path`` iff the path does not already exist.

    Uses ``O_CREAT | O_EXCL`` so the existence check and the write are one
    atomic kernel operation — no TOCTOU window between a prior ``.exists()``
    check and the write itself. Raises :class:`ManifestAlreadyExistsError` if
    the path already exists (races included: two concurrent writers targeting
    the same path always leave exactly one winner and one raised error, never
    a silent overwrite).
    """
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as e:
        raise ManifestAlreadyExistsError(
            f"{path} already exists; refusing to overwrite a prior attempt's record."
        ) from e
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def store_attempt_manifest(manifest: dict[str, Any]) -> tuple[Path, str]:
    """Writes the manifest JSON bytes + its digest beside the trace directory.

    Create-only under concurrency: both the manifest and digest files are
    written via ``O_CREAT | O_EXCL`` (:func:`_create_only_write`), so a
    concurrent writer targeting the same ``(trace_id, checkpoint)`` can never
    silently overwrite an earlier attempt's stored manifest (requirement:
    "never overwrite a prior attempt's manifest/trace file") — the loser of
    the race raises :class:`ManifestAlreadyExistsError` instead. One file is
    written per checkpoint (``launch`` / ``worker_start`` / ``termination``),
    keyed by ``trace_id``+``checkpoint``, so all three checkpoints for one
    attempt can coexist and each is independently immutable once written.

    Returns ``(manifest_path, digest_hex)``.
    """
    trace_id = manifest["trace_id"]
    checkpoint = manifest["checkpoint"]
    manifest_path, digest_path = _manifest_paths(trace_id, checkpoint)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()

    _create_only_write(manifest_path, payload)
    try:
        _create_only_write(digest_path, digest.encode("utf-8"))
    except ManifestAlreadyExistsError:
        # The manifest write above already won its race and is on disk and
        # immutable; only the digest file lost its race. Leave both files as
        # they are — the manifest write must not be undone (a concurrent
        # reader may already observe it), and re-raising surfaces the
        # conflict to the caller instead of masking it.
        raise

    return manifest_path, digest


def read_stored_manifest(trace_id: str, checkpoint: str) -> tuple[bytes, str] | None:
    """Reads back a previously stored manifest's exact bytes and digest, or None."""
    manifest_path, digest_path = _manifest_paths(trace_id, checkpoint)
    if not manifest_path.exists() or not digest_path.exists():
        return None
    return manifest_path.read_bytes(), digest_path.read_text(encoding="utf-8").strip()
