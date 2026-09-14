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

"""Reconciles native ``llm_usage`` trace events against an attempt manifest,
and applies the Gate 1 batch accept/reject rule across one or more attempts.

This module implements the *mechanism* only (Gate 1). It does not run or
claim to have run a qualification pilot; see
``artemis.config.attempt_manifest`` module docstring for the same caveat.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Literal

from artemis.config.attempt_manifest import digest_of, read_stored_manifest
from artemis.config.attempt_usage_reader import read_llm_usage_events
from artemis.runtime import trace_store

#: Machine-readable batch-reject reasons, verbatim per the Gate 1 spec.
RejectReason = Literal[
    "missing_identity",
    "unmapped_call",
    "mixed_tier",
    "unexpected_model_or_endpoint",
    "fake_mode_enabled",
    "digest_drift",
    "missing_snapshot",
]

REJECT_REASONS: tuple[RejectReason, ...] = (
    "missing_identity",
    "unmapped_call",
    "mixed_tier",
    "unexpected_model_or_endpoint",
    "fake_mode_enabled",
    "digest_drift",
    "missing_snapshot",
)

# Maps a usage-side node name (the `node` field a `@trace`-scoped call site
# actually records via CURRENT_NODE_NAME, see
# artemis.services.token_meter.record_llm_usage) to the manifest node name it
# should reconcile against, for the rare cases where the enclosing @trace
# scope's name legitimately differs from the LLMConfig field name the node
# resolves through.
#
# validator_pixel_safety_net is the one confirmed case today:
# artemis.agents.validator.validator._validate_action_precondition_pixel is
# traced as "safety_net_pixel_validation" (see task_tree.py's custom
# rendering, which depends on that exact trace name for unrelated UI
# purposes -- do not rename the @trace scope to "fix" this), but internally
# calls get_llm_fn(ctx, name="validator_pixel_safety_net"), which is the
# LLMConfig field / manifest node name. Without this alias, a real,
# correctly-identified LLM call reconciles as unmapped_call purely because
# the trace scope name and the manifest node name disagree.
_NODE_ALIASES: dict[str, str] = {
    "safety_net_pixel_validation": "validator_pixel_safety_net",
}


@dataclasses.dataclass(frozen=True)
class NodeReconciliation:
    """Reconciliation verdict for one manifest node against usage events."""

    node: str
    manifest_enabled: bool
    manifest_source: str | None  # "provider:model" the manifest expects, if enabled
    invoked: bool
    usage_sources: tuple[str, ...]  # distinct `source` values seen for this node
    verdict: Literal[
        "match",  # invoked, source matches manifest
        "mismatch",  # invoked, source present but does not match manifest
        "unverified_identity",  # invoked, but usage event(s) missing `source`
        "not_invoked",  # enabled in manifest, no usage event this attempt (informational)
        "disabled_not_invoked",  # explicitly disabled in manifest, and never invoked (expected)
        "unmapped_call",  # invoked, but node absent from manifest entirely
    ]


@dataclasses.dataclass(frozen=True)
class ReconciliationResult:
    """Full reconciliation outcome for one attempt: per-node verdicts + flags."""

    attempt_id: str
    nodes: tuple[NodeReconciliation, ...]

    @property
    def has_mismatch(self) -> bool:
        return any(n.verdict == "mismatch" for n in self.nodes)

    @property
    def has_unverified_identity(self) -> bool:
        return any(n.verdict == "unverified_identity" for n in self.nodes)

    @property
    def has_unmapped_call(self) -> bool:
        return any(n.verdict == "unmapped_call" for n in self.nodes)


def _manifest_expected_source(node_entry: dict[str, Any]) -> str | None:
    """The `provider:model` string a manifest node is expected to produce as `source`.

    For an enabled node this is its own ``config``. For a *disabled*
    (soft-defaulted) node -- e.g. ``validator_pixel_safety_net`` /
    ``planner_validation`` left unset in ``LLMConfig``, which
    ``LLMConfig.get_agent()`` actually resolves to
    ``lightweight_judge_default()`` (or an inherited node) when invoked, see
    ``attempt_manifest._build_node_entry`` -- the expected source is derived
    from ``would_resolve_to`` instead, since that is what the node will
    genuinely produce if it fires. A disabled node with no ``would_resolve_to``
    (no soft default applies; it is simply off) still returns ``None``: an
    unexpected receipt from an entirely-disabled node must still surface as a
    problem, not be silently accepted.
    """
    config = node_entry.get("config")
    if node_entry.get("enabled"):
        config = config or {}
    else:
        config = node_entry.get("would_resolve_to") or None
        if config is None:
            return None
    provider = config.get("provider")
    model = config.get("model")
    if provider is None or model is None:
        return None
    return f"{provider}:{model}"


def reconcile_attempt(
    attempt_id: str,
    manifest: dict[str, Any],
    usage_events: list[dict[str, Any]],
) -> ReconciliationResult:
    """Reconciles one attempt's manifest against its raw ``llm_usage`` payloads.

    ``usage_events`` are the dicts produced by
    ``artemis.services.token_meter.record_llm_usage`` (carrying at least
    ``node``; ``source`` is only present when the call site passed one — see
    ``RobustChatModelWrapper._endpoint_key()`` for the ``provider:model``
    shape, and the ``lens:*`` labels used by raw-model bypass call sites in
    ``memory/chunking.py`` / ``agents/flash/summarizer.py``, which are
    reported as-is and will not match any manifest node's ``provider:model``
    expectation — they surface as ``mismatch`` rather than being silently
    accepted, since Gate 1 requires every invoked identity to be provable,
    not merely present).

    Never fabricates a usage record for a node that did not fire: nodes with
    no usage event are reported as ``not_invoked``/``disabled_not_invoked``,
    not synthesized as zero-usage matches.
    """
    manifest_nodes: dict[str, dict[str, Any]] = {
        **manifest.get("nodes", {}),
        **manifest.get("utils", {}),
    }

    events_by_node: dict[str, list[dict[str, Any]]] = {}
    for event in usage_events:
        node = event.get("node")
        if node is None:
            # A usage event with no node context at all cannot be attributed
            # to any manifest entry; treat its node key as the literal
            # sentinel so it still surfaces as unmapped rather than being
            # dropped silently.
            node = "<unknown-node>"
        node = str(node)
        # Resolve to the manifest node name when the usage-side node is a
        # known trace-scope alias (see _NODE_ALIASES); otherwise group under
        # the raw node name as before.
        resolved_node = _NODE_ALIASES.get(node, node)
        events_by_node.setdefault(resolved_node, []).append(event)

    results: list[NodeReconciliation] = []
    seen_nodes: set[str] = set()

    for node, entry in manifest_nodes.items():
        seen_nodes.add(node)
        expected_source = _manifest_expected_source(entry)
        events = events_by_node.get(node, [])
        if not events:
            verdict = "not_invoked" if entry.get("enabled") else "disabled_not_invoked"
            results.append(
                NodeReconciliation(
                    node=node,
                    manifest_enabled=bool(entry.get("enabled")),
                    manifest_source=expected_source,
                    invoked=False,
                    usage_sources=(),
                    verdict=verdict,
                )
            )
            continue

        sources = tuple(e.get("source") for e in events)
        if any(s is None for s in sources):
            verdict = "unverified_identity"
        elif expected_source is not None and any(s != expected_source for s in sources):
            verdict = "mismatch"
        elif expected_source is None:
            # Node fired but the manifest has no resolvable provider:model to
            # expect for it -- either an enabled entry missing config, or a
            # disabled node with no soft default (see
            # `_manifest_expected_source`). A soft-defaulted disabled node
            # (e.g. validator_pixel_safety_net left unset) still has an
            # `expected_source` here, derived from `would_resolve_to`, so it
            # is *not* covered by this branch and can legitimately reach
            # "match" below. Surface this branch as mismatch rather than
            # pretending an entirely-disabled node firing is fine.
            verdict = "mismatch"
        else:
            verdict = "match"

        results.append(
            NodeReconciliation(
                node=node,
                manifest_enabled=bool(entry.get("enabled")),
                manifest_source=expected_source,
                invoked=True,
                usage_sources=tuple(s for s in sources if s is not None),
                verdict=verdict,
            )
        )

    # Usage events for nodes the manifest never declared at all (e.g. a
    # renamed node, a lens/utility label, or an entirely unmapped call site).
    for node, events in events_by_node.items():
        if node in seen_nodes:
            continue
        sources = tuple(e.get("source") for e in events if e.get("source") is not None)
        results.append(
            NodeReconciliation(
                node=node,
                manifest_enabled=False,
                manifest_source=None,
                invoked=True,
                usage_sources=sources,
                verdict="unmapped_call",
            )
        )

    return ReconciliationResult(attempt_id=attempt_id, nodes=tuple(results))


# ==============================================================================
# Batch validation / rejection rule
# ==============================================================================


@dataclasses.dataclass(frozen=True)
class AttemptRecord:
    """One attempt's manifest + reconciliation, as handed to batch validation."""

    attempt_id: str
    manifest: dict[str, Any]
    reconciliation: ReconciliationResult
    # Digest computed at build/store time (from attempt_manifest.digest_of),
    # supplied by the caller so batch validation can detect drift between
    # what was stored and what is being validated now, without recomputing
    # trust in a possibly-tampered manifest dict.
    stored_digest: str | None = None


@dataclasses.dataclass(frozen=True)
class BatchVerdict:
    """ACCEPT, or REJECT with a specific machine-readable reason.

    On reject, ``invalid_attempts`` preserves the exact records that failed
    (manifest + reconciliation unmodified) so a caller can retain them for
    audit rather than discarding them from totals.
    """

    accepted: bool
    reason: RejectReason | None
    detail: str
    invalid_attempts: tuple[AttemptRecord, ...] = ()


def _reject(reason: RejectReason, detail: str, invalid: tuple[AttemptRecord, ...]) -> BatchVerdict:
    return BatchVerdict(accepted=False, reason=reason, detail=detail, invalid_attempts=invalid)


def validate_batch(attempts: list[AttemptRecord]) -> BatchVerdict:
    """Validates one homogeneous batch of attempts (e.g. one journey x device cell).

    Checks run in a fixed order so the first violation found is reported;
    every check preserves the offending attempt record(s) unmodified in
    ``invalid_attempts`` rather than discarding them. An empty batch is
    reported as ``missing_snapshot`` (nothing to validate is itself a defect
    for a caller expecting N repeats).
    """
    if not attempts:
        return _reject("missing_snapshot", "Batch is empty: no attempt manifests supplied.", ())

    # 1. missing_identity: any attempt whose manifest lacks a usable
    #    source_sha, whose fake_llm flag/tier is absent entirely, whose
    #    source_sha was never actually verified against a real git checkout
    #    (source_sha_provenance != "git" -- the "0"*40/"unknown-not-a-git-
    #    checkout" fallback sentinel is truthy but not a provable identity),
    #    or that has any enabled node resolving to no declared tier at all
    #    (untiered_enabled_nodes non-empty -- a model-bearing node that isn't
    #    pinned to Luna/Terra/Sol).
    missing_identity = [
        a
        for a in attempts
        if not a.manifest.get("source_sha")
        or a.manifest.get("tier") not in ("luna", "terra", "sol")
        or a.manifest.get("fake_llm_enabled") is None
        or a.manifest.get("source_sha_provenance") != "git"
        or a.manifest.get("untiered_enabled_nodes")
    ]
    if missing_identity:
        return _reject(
            "missing_identity",
            f"{len(missing_identity)} attempt(s) have an incomplete manifest identity "
            "(missing source_sha, tier, or fake_llm_enabled; an unverified/non-git "
            "source_sha; or an enabled node with no declared tier).",
            tuple(missing_identity),
        )

    # 2. fake_mode_enabled: any attempt that ran with ARTEMIS_FAKE_LLM=1.
    fake_enabled = [a for a in attempts if a.manifest.get("fake_llm_enabled")]
    if fake_enabled:
        env_var = fake_enabled[0].manifest.get("fake_llm_env_var", "ARTEMIS_FAKE_LLM")
        return _reject(
            "fake_mode_enabled",
            f"{len(fake_enabled)} attempt(s) ran with fake-LLM mode enabled ({env_var}=1).",
            tuple(fake_enabled),
        )

    # 3. missing_snapshot: any attempt with no stored digest to compare against.
    missing_snapshot = [a for a in attempts if not a.stored_digest]
    if missing_snapshot:
        return _reject(
            "missing_snapshot",
            f"{len(missing_snapshot)} attempt(s) have no stored manifest digest to verify.",
            tuple(missing_snapshot),
        )

    # 4. digest_drift: recomputed digest of the manifest dict no longer
    #    matches the digest recorded at store time.
    drifted = [a for a in attempts if digest_of(a.manifest) != a.stored_digest]
    if drifted:
        return _reject(
            "digest_drift",
            f"{len(drifted)} attempt(s) have a manifest whose recomputed digest no longer "
            "matches its stored digest (possible post-hoc mutation).",
            tuple(drifted),
        )

    # 5. mixed_tier: not every attempt in the batch declares the same tier.
    tiers = {a.manifest.get("tier") for a in attempts}
    if len(tiers) > 1:
        return _reject(
            "mixed_tier",
            f"Batch mixes tiers across attempts: {sorted(t for t in tiers if t)}.",
            tuple(attempts),
        )

    # 6. unmapped_call / unexpected_model_or_endpoint: derived from each
    #    attempt's reconciliation result.
    unmapped = [a for a in attempts if a.reconciliation.has_unmapped_call]
    if unmapped:
        return _reject(
            "unmapped_call",
            f"{len(unmapped)} attempt(s) invoked a node absent from their manifest.",
            tuple(unmapped),
        )

    unexpected = [
        a
        for a in attempts
        if a.reconciliation.has_mismatch or a.reconciliation.has_unverified_identity
    ]
    if unexpected:
        return _reject(
            "unexpected_model_or_endpoint",
            f"{len(unexpected)} attempt(s) invoked a node whose observed `source` does not "
            "match the manifest, or is missing entirely (unverified identity).",
            tuple(unexpected),
        )

    return BatchVerdict(accepted=True, reason=None, detail=f"{len(attempts)} attempt(s) accepted.")


# ==============================================================================
# Post-run orchestration: stored manifest + native usage receipts -> verdict
# ==============================================================================


def reconcile_finished_attempt(
    *,
    trace_id: str,
    session_id: str,
    checkpoint: str,
    db_path: str | Path,
    traces_dir: str | Path,
) -> BatchVerdict | None:
    """Reconciles one finished attempt against its own stored manifest + DB receipts.

    Reads back the manifest :func:`artemis.config.attempt_manifest.store_attempt_manifest`
    wrote for ``(trace_id, checkpoint)``, reads every native ``llm_usage`` row
    the DataEngine recorded for ``session_id`` (via
    :func:`artemis.config.attempt_usage_reader.read_llm_usage_events`, never
    mutated or summarized), reconciles them, and runs the single-attempt batch
    rule. Returns ``None`` (not a verdict) when no manifest was stored for
    this checkpoint — that is a Gate 1 wiring gap to fix, not something to
    misrepresent as a passing or failing verdict.

    This is intentionally a one-attempt batch: multi-attempt batch validation
    (e.g. a five-repeat journey/device cell) is the qualification pilot's own
    concern, not something this per-run hook can see on its own.

    Because this always validates a batch of exactly one attempt, the
    ``mixed_tier`` rejection path is not reachable from this function; see
    :func:`reconcile_attempt_batch_by_run_id` for the production-reachable
    multi-attempt path.
    """
    stored = read_stored_manifest(trace_id, checkpoint)
    if stored is None:
        return None
    manifest_bytes, stored_digest = stored
    manifest = json.loads(manifest_bytes.decode("utf-8"))

    usage_events = read_llm_usage_events(db_path, traces_dir, session_id)
    reconciliation = reconcile_attempt(trace_id, manifest, usage_events)
    record = AttemptRecord(
        attempt_id=trace_id,
        manifest=manifest,
        reconciliation=reconciliation,
        stored_digest=stored_digest,
    )
    return validate_batch([record])


def reconcile_attempt_batch_by_run_id(
    *,
    run_id: str,
    checkpoint: str,
    db_path: str | Path,
    traces_dir: str | Path,
    traces_root: str | Path | None = None,
) -> BatchVerdict:
    """Reconciles every stored attempt sharing ``run_id`` as one batch.

    This is the production-reachable home for cross-attempt validation
    (including the ``mixed_tier`` rejection, which :func:`reconcile_finished_attempt`
    can never exercise since it always validates a batch of exactly one
    attempt). Today's ``mcp_server.background.task_runner`` still records
    exactly one attempt manifest per ``run_id`` (``run_id=trace_id`` 1:1, see
    ``_record_attempt_manifest``), so calling this with today's task runner
    output degenerates to a one-attempt batch. It is not wired into
    ``task_runner.py`` and is not currently invoked in production: it exists
    as a real, tested, importable, general-purpose multi-attempt reconciler
    that a future caller (e.g. a qualification-pilot orchestrator running
    repeated attempts of one journey/device cell under a shared ``run_id``)
    can call without needing new plumbing.

    Discovers candidate attempts by listing the immediate subdirectories of
    ``traces_root`` (default: :data:`artemis.runtime.trace_store.TRACES_DIR`)
    -- each subdirectory name is a ``trace_id``
    (:func:`artemis.runtime.trace_store.get_trace_dir`) -- reading each one's
    stored manifest at ``checkpoint`` via
    :func:`artemis.config.attempt_manifest.read_stored_manifest`, and keeping
    only the attempts whose manifest ``"run_id"`` field equals the requested
    ``run_id``. For each matching attempt, native ``llm_usage`` events are
    read via :func:`artemis.config.attempt_usage_reader.read_llm_usage_events`
    using the attempt's own ``trace_id`` as ``session_id`` (matching the
    ``session_id=trace_id`` convention ``_record_attempt_manifest`` /
    ``Agent(config=config, session_id=trace_id)`` already use), reconciled,
    and assembled into an :class:`AttemptRecord` exactly as
    :func:`reconcile_finished_attempt` does per-attempt.

    Read-only: never writes, mutates, or deletes any manifest or trace file.
    A directory whose stored manifest cannot be parsed as JSON is skipped
    rather than aborting the whole scan -- one corrupt/unreadable record must
    not hide every other attempt's evidence, mirroring
    ``read_llm_usage_events``'s own skip-corrupt-rows behavior.

    ``validate_batch`` is called over the full collected list, which may be
    empty (no attempt manifests found for ``run_id``); an empty batch
    correctly hits ``validate_batch``'s existing ``missing_snapshot``
    rejection rather than being special-cased here.
    """
    root = Path(traces_root) if traces_root is not None else Path(trace_store.TRACES_DIR)

    records: list[AttemptRecord] = []
    if root.is_dir():
        for trace_dir in sorted(root.iterdir()):
            if not trace_dir.is_dir():
                continue
            trace_id = trace_dir.name

            stored = read_stored_manifest(trace_id, checkpoint)
            if stored is None:
                continue
            manifest_bytes, stored_digest = stored
            try:
                manifest = json.loads(manifest_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A corrupt stored manifest for one attempt must not abort
                # discovery of every other attempt sharing this run_id.
                continue
            if manifest.get("run_id") != run_id:
                continue

            usage_events = read_llm_usage_events(db_path, traces_dir, trace_id)
            reconciliation = reconcile_attempt(trace_id, manifest, usage_events)
            records.append(
                AttemptRecord(
                    attempt_id=trace_id,
                    manifest=manifest,
                    reconciliation=reconciliation,
                    stored_digest=stored_digest,
                )
            )

    return validate_batch(records)
