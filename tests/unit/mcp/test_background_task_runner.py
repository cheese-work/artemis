import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_server.background.task_runner import (
    _build_arg_parser,
    _initialize_agent,
    _reconcile_and_store_verdict,
    _record_attempt_manifest,
)


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


def test_cli_arg_parser_accepts_run_id_flag():
    """The real `__main__` CLI surface must accept --run-id without exiting 2.

    Reproduces the exact repro from the wiring-gap review: the CLI parser
    used to have no --run-id flag at all, so passing it would fail argparse
    with "unrecognized arguments" (exit code 2) before run_task was ever
    called. Exercises _build_arg_parser() directly -- the same
    ArgumentParser the `if __name__ == "__main__":` block constructs -- so
    this proves the CLI surface itself, not just the in-process run_task
    Python parameter.
    """
    parser = _build_arg_parser()

    args = parser.parse_args(
        [
            "--trace-id",
            "trace-abc",
            "--task-desc",
            "Do the thing",
            "--model",
            "Flash",
            "--run-id",
            "shared-run-xyz",
        ]
    )

    assert args.run_id == "shared-run-xyz"

    # Omitting --run-id must still parse cleanly and default to None, so an
    # unmodified CLI invocation retains today's exact 1:1 run_id<->trace_id
    # behavior in run_task.
    args_no_run_id = parser.parse_args(
        ["--trace-id", "trace-abc", "--task-desc", "Do the thing", "--model", "Flash"]
    )
    assert args_no_run_id.run_id is None


class TestTerminationTimeBatchReconciliation:
    """Proves _reconcile_and_store_verdict -- the function the finally: block
    in run_task actually calls at termination -- genuinely fires batch
    reconciliation and rejects a real mixed-tier batch, not merely that
    reconcile_attempt_batch_by_run_id exists and can be called directly (that
    is already covered by TestRealWorkerMultiAttemptRunIdGrouping and the
    config-level TestReconcileAttemptBatchByRunId tests).

    This closes the second half of the wiring gap: even with --run-id
    plumbed through the CLI, nothing previously called the batch reconciler
    at termination -- the finally: block always called single-attempt
    reconciliation only.
    """

    def test_termination_reconciliation_writes_mixed_tier_batch_verdict(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from artemis.config.constants import ENV_ARTEMIS_TRACES_DIR
        from artemis.data_engine.models import SessionMetadata, TraceRecord
        from artemis.data_engine.storage import StorageManager
        from artemis.runtime import trace_store

        traces_dir = tmp_path / "traces"
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(traces_dir))
        # get_data_engine_db_path() / get_traces_dir() (called internally by
        # _reconcile_and_store_verdict, not injectable) must resolve to the
        # same tmp_path tree the manifests and llm_usage receipts below are
        # written under.
        monkeypatch.setenv(ENV_ARTEMIS_TRACES_DIR, str(traces_dir))
        monkeypatch.setenv("ARTEMIS_FAKE_LLM", "0")

        run_id = "shared-termination-run-id"
        sol_trace_id = "termination-attempt-sol"
        terra_trace_id = "termination-attempt-terra"

        sol_llm_config = _uniform_llm_config("openai", "gpt-5.6-sol")
        terra_llm_config = _uniform_llm_config("google", "gemini-3.8-flash")

        # First attempt: same real _record_attempt_manifest call run_task
        # makes at its "launch" checkpoint.
        _record_attempt_manifest(
            trace_id=sol_trace_id, checkpoint="launch", llm_config=sol_llm_config, run_id=run_id
        )
        _record_attempt_manifest(
            trace_id=terra_trace_id,
            checkpoint="launch",
            llm_config=terra_llm_config,
            run_id=run_id,
        )

        from artemis.config.paths import get_data_engine_db_path

        db_path = get_data_engine_db_path()
        storage = StorageManager(db_path, traces_dir)
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

        # The exact call run_task's finally: block makes at termination for
        # the second (terra) attempt, now with a real, shared run_id -- this
        # is the wiring under test, not a direct
        # reconcile_attempt_batch_by_run_id() call.
        _reconcile_and_store_verdict(trace_id=terra_trace_id, checkpoint="launch", run_id=run_id)

        batch_verdict_path = (
            Path(trace_store.get_trace_dir(terra_trace_id))
            / "attempt_reconciliation_batch_verdict.json"
        )
        assert batch_verdict_path.exists(), (
            "termination-time _reconcile_and_store_verdict did not write a batch verdict "
            "file even though a real, non-default run_id was passed"
        )

        payload = json.loads(batch_verdict_path.read_text(encoding="utf-8"))
        assert payload["accepted"] is False
        assert payload["reason"] == "mixed_tier"

    def test_termination_reconciliation_skips_batch_when_run_id_matches_trace_id(
        self, tmp_path, monkeypatch
    ):
        """Default single-attempt behavior (run_id omitted / equal to trace_id)
        must not produce a batch verdict file at all -- proves the batch path
        is genuinely additive and does not fire for today's unmodified callers.
        """
        from pathlib import Path

        from artemis.config.constants import ENV_ARTEMIS_TRACES_DIR
        from artemis.runtime import trace_store

        traces_dir = tmp_path / "traces"
        monkeypatch.setattr(trace_store, "TRACES_DIR", str(traces_dir))
        monkeypatch.setenv(ENV_ARTEMIS_TRACES_DIR, str(traces_dir))
        monkeypatch.setenv("ARTEMIS_FAKE_LLM", "0")

        trace_id = "solo-attempt"
        llm_config = _uniform_llm_config("openai", "gpt-5.6-sol")
        _record_attempt_manifest(trace_id=trace_id, checkpoint="launch", llm_config=llm_config)

        # No manifest for this trace_id sharing a run_id exists other than
        # itself, and run_id defaults to trace_id inside
        # _record_attempt_manifest -- mirrors an unmodified caller exactly.
        _reconcile_and_store_verdict(trace_id=trace_id, checkpoint="launch", run_id=None)

        batch_verdict_path = (
            Path(trace_store.get_trace_dir(trace_id)) / "attempt_reconciliation_batch_verdict.json"
        )
        assert not batch_verdict_path.exists()
