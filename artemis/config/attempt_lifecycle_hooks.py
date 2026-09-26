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

"""Shared best-effort Gate 1 evidence hooks for every real execution path.

Both the standalone/worker-subprocess runner (``mcp_server.background.task_runner``)
and the daemon-dispatched / direct-CLI runner (``artemis.interfaces.cli.commands.run``)
build an ``Agent`` from an ``LLMConfig`` and call ``agent.run_task`` under a shared
``trace_id``. This module gives both call sites the exact same
record-manifest-before / reconcile-after contract so a Gate 1 evidence gap is never a
function of which entry point started the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def record_attempt_manifest(
    *,
    trace_id: str,
    checkpoint: str,
    llm_config: Any,
    run_id: str | None = None,
    parent_attempt_id: str | None = None,
) -> None:
    """Best-effort Gate 1 evidence hook: resolve, hash and store one attempt
    manifest checkpoint beside this trace before the real inference run.

    Mirrors ``token_meter.record_llm_usage``'s own contract: this must never
    raise into the task execution path. A tier that cannot be inferred from
    ``llm_config`` (this fork's ``config/artemis.jsonc`` does not yet declare
    an explicit tier table -- see ``attempt_manifest.TIER_MODELS``) or a
    checkpoint that collides with an already-stored one (create-only storage)
    are both recorded as skips, not failures, so a manifest gap is visible in
    the caller's own log without ever aborting a live mobile task.

    ``run_id`` defaults to ``trace_id`` when not given, preserving the exact
    1:1 ``run_id``<->``trace_id`` behavior every existing caller relies on. A
    caller that wants several real attempts to be reconcilable together as
    one batch (see ``attempt_reconciliation.reconcile_attempt_batch_by_run_id``)
    can pass a ``run_id`` shared across multiple calls instead.
    """
    try:
        from artemis.config.attempt_manifest import (
            TIER_MODELS,
            ManifestAlreadyExistsError,
            build_attempt_manifest,
            store_attempt_manifest,
        )
        import os

        tier = None
        for candidate_tier, (provider, model_name) in TIER_MODELS.items():
            if llm_config.planner.provider == provider and llm_config.planner.model == model_name:
                tier = candidate_tier
                break
        if tier is None:
            print(
                f"Attempt manifest [{checkpoint}] skipped: planner "
                f"{llm_config.planner.provider}/{llm_config.planner.model} does not match "
                "a declared tier in attempt_manifest.TIER_MODELS."
            )
            return

        manifest = build_attempt_manifest(
            run_id=run_id or trace_id,
            attempt_id=trace_id,
            trace_id=trace_id,
            tier=tier,
            llm_config=llm_config,
            env=dict(os.environ),
            checkpoint=checkpoint,
            parent_attempt_id=parent_attempt_id,
        )
        manifest_path, digest = store_attempt_manifest(manifest)
        print(f"Attempt manifest [{checkpoint}] stored at {manifest_path} (sha256={digest}).")
    except ManifestAlreadyExistsError as exc:
        print(f"Attempt manifest [{checkpoint}] skipped: {exc}")
    except Exception as exc:
        print(f"Attempt manifest [{checkpoint}] skipped (best-effort, non-fatal): {exc}")


def reconcile_and_store_verdict(
    *, trace_id: str, checkpoint: str = "launch", run_id: str | None = None
) -> None:
    """Best-effort Gate 1 evidence hook: reconcile this attempt's stored
    manifest against its native ``llm_usage`` receipts and preserve the
    verdict beside the trace.

    Runs after the real inference run returns, regardless of task outcome --
    identity verification is orthogonal to whether the mobile task itself
    passed or failed. Never raises into the task path (same best-effort
    contract as :func:`record_attempt_manifest`). Preserves the original
    ``llm_usage`` receipts unmodified: this only adds a derived verdict file,
    it never rewrites or drops the native rows the DataEngine already wrote.

    When ``run_id`` is given and genuinely differs from ``trace_id`` (i.e.
    this attempt was launched as part of an explicit multi-attempt batch, not
    the default 1:1 case), this also runs
    ``attempt_reconciliation.reconcile_attempt_batch_by_run_id`` across every
    stored attempt sharing that ``run_id`` and stores that batch verdict as a
    sibling file, ``attempt_reconciliation_batch_verdict.json``, next to the
    single-attempt verdict -- kept separate so neither file's meaning is
    ambiguous. The batch call is wrapped in its own best-effort guard so a
    batch-reconciliation failure can never suppress the single-attempt
    verdict this function already stored.
    """
    from artemis.runtime import trace_store

    try:
        from artemis.config.attempt_reconciliation import reconcile_finished_attempt
        from artemis.config.paths import get_data_engine_db_path, get_traces_dir

        verdict = reconcile_finished_attempt(
            trace_id=trace_id,
            session_id=trace_id,
            checkpoint=checkpoint,
            db_path=get_data_engine_db_path(),
            traces_dir=get_traces_dir(),
        )
        if verdict is None:
            print(
                f"Attempt reconciliation skipped: no stored manifest for trace {trace_id!r} "
                f"checkpoint {checkpoint!r}."
            )
        else:
            trace_dir = Path(trace_store.get_existing_trace_dir(trace_id))
            verdict_path = trace_dir / "attempt_reconciliation_verdict.json"
            verdict_payload = {
                "accepted": verdict.accepted,
                "reason": verdict.reason,
                "detail": verdict.detail,
                "invalid_attempt_count": len(verdict.invalid_attempts),
            }
            verdict_path.write_text(json.dumps(verdict_payload, indent=2), encoding="utf-8")
            print(
                f"Attempt reconciliation verdict stored at {verdict_path}: "
                f"accepted={verdict.accepted} reason={verdict.reason}."
            )
    except Exception as exc:
        print(f"Attempt reconciliation skipped (best-effort, non-fatal): {exc}")

    if run_id is not None and run_id != trace_id:
        try:
            from artemis.config.attempt_reconciliation import reconcile_attempt_batch_by_run_id
            from artemis.config.paths import get_data_engine_db_path, get_traces_dir

            batch_verdict = reconcile_attempt_batch_by_run_id(
                run_id=run_id,
                checkpoint=checkpoint,
                db_path=get_data_engine_db_path(),
                traces_dir=get_traces_dir(),
                traces_root=Path(trace_store.TRACES_DIR),
            )
            trace_dir = Path(trace_store.get_existing_trace_dir(trace_id))
            batch_verdict_path = trace_dir / "attempt_reconciliation_batch_verdict.json"
            batch_verdict_payload = {
                "accepted": batch_verdict.accepted,
                "reason": batch_verdict.reason,
                "detail": batch_verdict.detail,
                "invalid_attempt_count": len(batch_verdict.invalid_attempts),
            }
            batch_verdict_path.write_text(
                json.dumps(batch_verdict_payload, indent=2), encoding="utf-8"
            )
            print(
                f"Attempt batch reconciliation verdict stored at {batch_verdict_path}: "
                f"accepted={batch_verdict.accepted} reason={batch_verdict.reason}."
            )
        except Exception as exc:
            print(f"Attempt batch reconciliation skipped (best-effort, non-fatal): {exc}")
