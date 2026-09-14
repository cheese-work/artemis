import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.background.task_runner import _initialize_agent, _record_attempt_manifest


@pytest.mark.asyncio
async def test_background_agent_initialization_has_hard_timeout():
    async def slow_init(**_):
        await asyncio.sleep(60)

    agent = MagicMock()
    agent.init = AsyncMock(side_effect=slow_init)

    with pytest.raises(TimeoutError, match="initialization exceeded 0.0s"):
        await _initialize_agent(
            agent,
            retry_count=1,
            retry_wait_seconds=1,
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_background_agent_initialization_forwards_health_settings():
    agent = AsyncMock()

    await _initialize_agent(
        agent,
        retry_count=3,
        retry_wait_seconds=4,
        timeout_seconds=1.0,
    )

    agent.init.assert_awaited_once_with(retry_count=3, retry_wait_seconds=4)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("knobs", "expect_level", "expect_mode"),
    [
        ({"verification_level": "strict", "explorer_pro_mode": "ultra"}, "strict", "ultra"),
        ({}, None, None),
    ],
)
async def test_run_task_applies_pro_tuning_to_agent_config(
    tmp_path, monkeypatch, knobs, expect_level, expect_mode
):
    """The detached runner applies --verification-level / --explorer-pro-mode on the builder."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from artemis.runtime import trace_store
    from mcp_server.background import task_runner as bg

    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    trace_id = "trace-tuning"
    trace_store.init_trace(
        trace_id=trace_id,
        task_desc="Audit checkout",
        model="Pro",
        conversation_id="",
        device_serial="emulator-5554",
    )

    fake_builder = MagicMock()
    fake_builders = MagicMock()
    fake_builders.AgentConfig.with_default_profile.return_value = fake_builder
    fake_agent = MagicMock()
    fake_agent._device_context = SimpleNamespace(device_id="emulator-5554")
    fake_agent.run_task = AsyncMock(return_value="done")
    fake_agent.clean = AsyncMock()

    monkeypatch.setattr(bg.device_utils, "resolve_adb_path", lambda: "adb")
    monkeypatch.setattr(bg.device_utils, "get_connected_devices", lambda _adb: ["emulator-5554"])
    monkeypatch.setattr(bg, "resolve_profile_file", lambda: None)
    monkeypatch.setattr(bg, "_initialize_agent", AsyncMock())
    monkeypatch.setattr(bg, "notify", MagicMock())

    with (
        patch("artemis.sdk.builders.Builders", fake_builders),
        patch("artemis.sdk.Agent", return_value=fake_agent),
        patch("artemis.sdk.types.AgentProfile", MagicMock()),
        patch("artemis.config.initialize_llm_config", return_value=MagicMock()),
    ):
        await bg.run_task(
            trace_id=trace_id,
            task_desc="Audit checkout",
            model="Pro",
            conversation_id="",
            device_serial="emulator-5554",
            **knobs,
        )

    if expect_level is None:
        fake_builder.with_verification_level.assert_not_called()
        fake_builder.with_explorer.assert_not_called()
    else:
        fake_builder.with_verification_level.assert_called_once_with(expect_level)
        fake_builder.with_explorer.assert_called_once_with(pro_mode=expect_mode)
    fake_agent.run_task.assert_awaited_once()
    assert trace_store.read_status(trace_id)["status"] == "completed"


def _uniform_llm_config(provider: str, model: str):
    """A fully-populated LLMConfig where every required node resolves to one tier.

    Mirrors ``tests/unit/config/test_attempt_reconciliation.py``'s
    ``_uniform_config`` helper -- kept local to this file since it exercises
    the real ``LLMConfig``, not a mock, so ``_record_attempt_manifest`` can
    genuinely infer a tier from ``llm_config.planner`` the same way the real
    ``run_task`` path does.
    """
    from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback

    node = LLMWithFallback(
        provider=provider, model=model, fallback=LLM(provider=provider, model=model)
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


class TestRealWorkerMultiAttemptRunIdGrouping:
    """Proves the real ``run_task``-facing ``run_id`` plumbing (not a
    synthetic ``build_attempt_manifest`` call) actually produces manifests
    that ``reconcile_attempt_batch_by_run_id`` groups together and rejects on
    ``mixed_tier`` -- the "real worker output reaches validate_batch" gap
    ``TestReconcileAttemptBatchByRunId`` (config-level, synthetic manifests
    only) cannot close on its own.

    Calls the real ``_record_attempt_manifest`` (the exact function
    ``run_task`` calls, with the same ``run_id`` parameter ``run_task`` now
    threads through to it) directly, twice, with a shared ``run_id`` but
    distinct ``trace_id``s and tiers -- mirroring this file's existing
    tmp_path-isolated ``TRACES_DIR`` pattern rather than driving the full
    (heavily mocked) SDK agent path a second time in one test.
    """

    def test_two_real_attempts_sharing_run_id_with_different_tiers_is_rejected_mixed_tier(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from artemis.config.attempt_reconciliation import reconcile_attempt_batch_by_run_id
        from artemis.data_engine.models import SessionMetadata, TraceRecord
        from artemis.data_engine.storage import StorageManager
        from artemis.runtime import trace_store

        monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
        monkeypatch.setenv("ARTEMIS_FAKE_LLM", "0")

        run_id = "shared-retry-run-id"
        sol_trace_id = "real-attempt-sol"
        terra_trace_id = "real-attempt-terra"

        sol_llm_config = _uniform_llm_config("openai", "gpt-5.6-sol")
        terra_llm_config = _uniform_llm_config("google", "gemini-3.8-flash")

        # The exact call run_task makes at its "launch" checkpoint, invoked
        # twice with the same run_id -- this is the real plumbing added to
        # thread run_id through, not a direct build_attempt_manifest() call.
        _record_attempt_manifest(
            trace_id=sol_trace_id, checkpoint="launch", llm_config=sol_llm_config, run_id=run_id
        )
        _record_attempt_manifest(
            trace_id=terra_trace_id,
            checkpoint="launch",
            llm_config=terra_llm_config,
            run_id=run_id,
        )

        db_path = tmp_path / "usage.db"
        usage_traces_dir = tmp_path / "usage_traces"
        storage = StorageManager(db_path, usage_traces_dir)
        for trace_id, source in (
            (sol_trace_id, "openai:gpt-5.6-sol"),
            (terra_trace_id, "google:gemini-3.8-flash"),
        ):
            storage.create_session(SessionMetadata(session_id=trace_id, initial_goal="test goal"))
            storage.create_trace(
                TraceRecord(
                    trace_id=f"usage-{trace_id}-planner",
                    session_id=trace_id,
                    type="llm_call",
                    name="llm_usage",
                    payload={"node": "planner", "source": source},
                )
            )

        verdict = reconcile_attempt_batch_by_run_id(
            run_id=run_id,
            checkpoint="launch",
            db_path=db_path,
            traces_dir=usage_traces_dir,
            traces_root=Path(trace_store.TRACES_DIR),
        )

        assert verdict.accepted is False
        assert verdict.reason == "mixed_tier"
        invalid_ids = {a.attempt_id for a in verdict.invalid_attempts}
        assert invalid_ids == {sol_trace_id, terra_trace_id}
