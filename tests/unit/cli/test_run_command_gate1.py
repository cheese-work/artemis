"""Gate 1 evidence regression tests for the daemon-dispatched / direct-CLI
``execute_task`` path (``artemis.interfaces.cli.commands.run``).

Covers three defects found in independent review of the initial daemon-path
wiring:

1. The default ``--standalone`` route (no explicit ``--session-id``) must
   still generate a canonical trace identity so a real inference run is
   never Gate-1-evidence-free by default.
2. ``reconcile_and_store_verdict`` must run before -- and regardless of --
   ``agent.clean()``, since ``Agent.clean()`` can raise (e.g. on device
   disconnect) and must never be able to suppress the reconciliation
   verdict for a real completed run.
3. A generated fallback identity from one default-route call must never
   leak into a later default-route call in the same process via the
   process-global ``ARTEMIS_SESSION_ID`` env var -- each attempt must get
   its own identity and its own stored manifest.
4. An explicit ``--session-id`` on one call must not contaminate a *later*
   default-route call either -- ``ARTEMIS_SESSION_ID`` must be restored to
   whatever it held before the call (or cleared), not just skipped for the
   generated-fallback case.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.interfaces.cli.commands import run as run_module


def _fake_llm_config():
    """A real, minimal LLMConfig (AgentProfile validates this via pydantic,
    so a bare MagicMock is rejected) -- provider/model values don't matter
    here since record_attempt_manifest/reconcile_and_store_verdict are
    patched out in every test below."""
    from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback

    node = LLMWithFallback(
        provider="google",
        model="gemini-3.8-flash",
        fallback=LLM(provider="google", model="gemini-3.8-flash"),
    )
    required_nodes = (
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
    return LLMConfig(
        **{n: node for n in required_nodes},
        utils=LLMConfigUtils(outputter=node, hopper=node),
    )


def _fake_agent(clean_side_effect=None):
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock(return_value="ok")
    agent.clean = AsyncMock(side_effect=clean_side_effect)
    task = MagicMock()
    task.build.return_value = "built-task"
    agent.new_task.return_value = task
    return agent


@pytest.mark.asyncio
async def test_execute_task_default_standalone_route_still_records_gate1_evidence(monkeypatch):
    """No --session-id, no env session vars: execute_task must still record
    and reconcile a real attempt under a generated trace identity, not skip
    Gate 1 evidence for the default route."""
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    monkeypatch.delenv("ARTEMIS_CLOUD_SESSION_ID", raising=False)
    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", "fake-test-key")

    recorded = []
    reconciled = []
    agent = _fake_agent()

    with (
        patch.object(run_module, "initialize_llm_config", return_value=_fake_llm_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(
            run_module,
            "record_attempt_manifest",
            side_effect=lambda **kw: recorded.append(kw["trace_id"]),
        ),
        patch.object(
            run_module,
            "reconcile_and_store_verdict",
            side_effect=lambda **kw: reconciled.append(kw["trace_id"]),
        ),
    ):
        await run_module.execute_task(goal="do the thing", session_id=None)

    assert len(recorded) == 1
    assert recorded[0]  # non-empty generated identity
    assert reconciled == recorded  # same trace_id reconciled as was recorded


@pytest.mark.asyncio
async def test_execute_task_two_default_calls_get_distinct_isolated_identities(monkeypatch):
    """Two back-to-back default-route calls (no --session-id, no env session
    vars) in the same process must each get their own generated trace id and
    each record their own manifest -- a generated fallback identity must
    never leak into the process env and get reused by a later default call,
    which would silently merge two attempts under one identity."""
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    monkeypatch.delenv("ARTEMIS_CLOUD_SESSION_ID", raising=False)
    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", "fake-test-key")

    recorded = []
    reconciled = []

    def _make_agent(*args, **kwargs):
        return _fake_agent()

    with (
        patch.object(run_module, "initialize_llm_config", return_value=_fake_llm_config()),
        patch.object(run_module, "Agent", side_effect=_make_agent),
        patch.object(
            run_module,
            "record_attempt_manifest",
            side_effect=lambda **kw: recorded.append(kw["trace_id"]),
        ),
        patch.object(
            run_module,
            "reconcile_and_store_verdict",
            side_effect=lambda **kw: reconciled.append(kw["trace_id"]),
        ),
    ):
        await run_module.execute_task(goal="first attempt", session_id=None)
        assert os.environ.get("ARTEMIS_SESSION_ID") is None
        await run_module.execute_task(goal="second attempt", session_id=None)
        assert os.environ.get("ARTEMIS_SESSION_ID") is None

    assert len(recorded) == 2
    assert recorded[0] and recorded[1]
    assert recorded[0] != recorded[1]  # distinct identities -> distinct manifests stored
    assert reconciled == recorded


@pytest.mark.asyncio
async def test_execute_task_explicit_session_id_does_not_contaminate_later_default_call(
    monkeypatch,
):
    """An explicit --session-id call followed by a default-route call in the
    same process must not collide: the first call's explicit identity must
    not leak via ARTEMIS_SESSION_ID and get picked up as the second call's
    "default" identity, which would merge two distinct attempts (and their
    create-only manifests) under one trace id."""
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    monkeypatch.delenv("ARTEMIS_CLOUD_SESSION_ID", raising=False)
    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", "fake-test-key")

    recorded = []
    reconciled = []

    def _make_agent(*args, **kwargs):
        return _fake_agent()

    with (
        patch.object(run_module, "initialize_llm_config", return_value=_fake_llm_config()),
        patch.object(run_module, "Agent", side_effect=_make_agent),
        patch.object(
            run_module,
            "record_attempt_manifest",
            side_effect=lambda **kw: recorded.append(kw["trace_id"]),
        ),
        patch.object(
            run_module,
            "reconcile_and_store_verdict",
            side_effect=lambda **kw: reconciled.append(kw["trace_id"]),
        ),
    ):
        await run_module.execute_task(goal="explicit attempt", session_id="explicit-sid-123")
        assert os.environ.get("ARTEMIS_SESSION_ID") is None

        await run_module.execute_task(goal="default attempt", session_id=None)
        assert os.environ.get("ARTEMIS_SESSION_ID") is None

    assert recorded == ["explicit-sid-123", recorded[1]]
    assert recorded[1] != "explicit-sid-123"  # default call got its own identity, not the leak
    assert reconciled == recorded


@pytest.mark.asyncio
async def test_execute_task_reconciles_before_cleanup_and_survives_cleanup_failure(monkeypatch):
    """A raising agent.clean() must not prevent reconcile_and_store_verdict
    from running, and reconciliation must be observed to happen first."""
    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", "fake-test-key")
    call_order = []
    agent = _fake_agent(clean_side_effect=RuntimeError("device disconnected"))
    agent.clean.side_effect = None  # set below after wrapping to record order

    async def clean_raises():
        call_order.append("clean")
        raise RuntimeError("device disconnected")

    agent.clean = AsyncMock(side_effect=clean_raises)

    def reconcile_records(**kw):
        call_order.append("reconcile")

    with (
        patch.object(run_module, "initialize_llm_config", return_value=_fake_llm_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict", side_effect=reconcile_records),
    ):
        # Must not raise: a cleanup failure must not propagate out of execute_task.
        await run_module.execute_task(goal="do the thing", session_id="fixed-sid")

    assert call_order == ["reconcile", "clean"]
