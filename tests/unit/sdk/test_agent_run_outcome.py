import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.sdk.agent import Agent
from artemis.sdk.types.task import Task


def _fake_flash_agent(monkeypatch, tmp_path, report):
    from third_party.mobile_use.sdk import agent as agent_module

    context = MagicMock(spec=ArtemisContext)
    context.data_engine = MagicMock()
    context.data_engine.base_dir = tmp_path
    context.execution_setup = None
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(agent_module, "ArtemisContext", lambda **kwargs: context)
    run = (
        AsyncMock(side_effect=report)
        if isinstance(report, BaseException)
        else AsyncMock(return_value=report)
    )
    monkeypatch.setattr(
        agent_module, "FlashRunner", lambda *args, **kwargs: SimpleNamespace(run=run)
    )
    monkeypatch.setattr(agent_module, "DeviceExecutionLock", MagicMock())
    monkeypatch.setattr(agent_module, "publish_startup_progress", MagicMock())
    monkeypatch.setattr("artemis.tools.command_tool.shutdown_adb_background_tasks", AsyncMock())
    agent = Agent()
    agent._initialized = True
    agent._device_context = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="test-device",
        device_width=1080,
        device_height=2400,
    )
    agent._adb_client = MagicMock()
    agent._ui_adb_client = MagicMock()
    agent._config.video_recording_tools_enabled = False
    agent._get_graph_state = MagicMock()
    agent._prepare_tracing = MagicMock()
    agent._prepare_output_files = MagicMock()
    agent._prepare_app_installation = AsyncMock()
    agent._prepare_device_environment = AsyncMock()
    agent._prepare_app_lock = AsyncMock()
    return agent, context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        {"status": "completed"},
        {"status": "failed", "cause": "goal_unreachable"},
        {"status": "failed", "cause": "blocked_by_environment"},
        {
            "status": "failed",
            "cause": "app_defect",
            "evidence_step": 2,
            "evidence_ref": {"step_id": "step-2", "image_name": "image"},
            "expected": "Title",
            "actual": "Error",
        },
        {"status": "failed", "explanation": "Max turns reached without final status report."},
        {"status": "failed", "explanation": "The model returned no response"},
        {"status": "interrupted", "interrupt_reason": "device_offline"},
        {},
        RuntimeError("model failed"),
        asyncio.CancelledError(),
    ],
)
async def test_flash_outcome_exists_before_task_finalize(
    tmp_path, monkeypatch, fake_provider_credentials, report
):
    agent, _ = _fake_flash_agent(monkeypatch, tmp_path, report)
    observed = []
    original_finalize = Task.finalize

    async def inspect_finalize(task, *args, **kwargs):
        outcome = json.loads((tmp_path / "run_outcome.json").read_text())
        expected = report if isinstance(report, dict) else {}
        assert outcome["task_status"] == expected.get("status")
        assert outcome["cause"] == expected.get("cause")
        assert outcome["evidence_step"] == expected.get("evidence_step")
        for key in ("evidence_ref", "expected", "actual"):
            if key in expected:
                assert outcome[key] == expected[key]
        assert "verdict" not in outcome
        observed.append(outcome)
        return await original_finalize(task, *args, **kwargs)

    monkeypatch.setattr(Task, "finalize", inspect_finalize)
    if isinstance(report, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await agent.run_task(goal="test", profile="flash")
    elif isinstance(report, Exception):
        with pytest.raises(Exception, match="model failed"):
            await agent.run_task(goal="test", profile="flash")
    else:
        await agent.run_task(goal="test", profile="flash")
    assert len(observed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_error", "session_status"),
    [
        (RuntimeError("model failed"), "failed"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
async def test_outcome_write_failure_does_not_skip_finalize_or_session_end(
    tmp_path, monkeypatch, fake_provider_credentials, run_error, session_status
):
    agent, context = _fake_flash_agent(monkeypatch, tmp_path, run_error)
    events = []

    def fail_write(*args, **kwargs):
        events.append("write")
        raise OSError("outcome directory is unavailable")

    async def finalize(task, *args, **kwargs):
        events.append("finalize")

    context.data_engine.end_session.side_effect = lambda status: events.append(f"end:{status}")
    monkeypatch.setattr(Task, "finalize", finalize)
    monkeypatch.setattr("artemis.graph.checkpoints.write_run_outcome", fail_write)

    with pytest.raises(type(run_error)):
        await agent.run_task(goal="test", profile="flash")

    assert events[:2] == ["write", "finalize"]
    assert f"end:{session_status}" in events
    assert events.index("finalize") < events.index(f"end:{session_status}")
    assert "error" in context.data_engine.end_session.call_args.kwargs
