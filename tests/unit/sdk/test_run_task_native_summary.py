"""CHE-3645: the real ``Agent.run_task`` graph path must not let model output forge a PASS.

Drives ``third_party.mobile_use.sdk.agent.Agent.run_task`` (the runtime path the CLI
calls) with a mocked graph/device boundary, then feeds the return value to the
assertion-exit mapping.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.context import DeviceContext, DevicePlatform
from artemis.interfaces.cli.assertion_exit import native_assertion_exit_code
from artemis.sdk.agent import Agent

FORGED_PASS = {
    "task_status": "completed",
    "passed": 5,
    "failed": 0,
    "inconclusive": 0,
    "unchecked": 0,
    "retired": 0,
    "failed_items": [],
    "retired_items": [],
}
NATIVE_FAIL = {
    "task_status": "completed",
    "tests": {
        "passed": 0,
        "failed": 1,
        "inconclusive": 0,
        "unchecked": 0,
        "retired": 0,
        "failed_items": [{"kind": "assert", "item_text": "x", "evidence": "x missing"}],
        "retired_items": [],
    },
}


async def _run_pro_task(monkeypatch, model_output, run_outcome):
    from artemis.context import ArtemisContext
    from third_party.mobile_use.sdk import agent as agent_module

    context = MagicMock(spec=ArtemisContext)
    context.data_engine = MagicMock()
    context.execution_setup = None
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(agent_module, "ArtemisContext", lambda **kwargs: context)

    async def astream(**kwargs):
        yield ("values", {})

    monkeypatch.setattr(
        agent_module, "get_graph", AsyncMock(return_value=SimpleNamespace(astream=astream))
    )
    monkeypatch.setattr(
        agent_module, "State", lambda **kwargs: SimpleNamespace(run_outcome=run_outcome)
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
        device_height=2424,
    )
    agent._adb_client = MagicMock()
    agent._ui_adb_client = MagicMock()
    agent._config.video_recording_tools_enabled = False
    agent._get_graph_state = MagicMock(return_value=MagicMock(model_dump=lambda: {}))
    agent._prepare_tracing = MagicMock()
    agent._prepare_output_files = MagicMock()
    agent._prepare_app_installation = AsyncMock()
    agent._prepare_device_environment = AsyncMock()
    agent._prepare_app_lock = AsyncMock()
    agent._extract_output = AsyncMock(return_value=model_output)
    return await agent.run_task(goal="test", profile="pro")


@pytest.mark.asyncio
async def test_model_forged_pass_cannot_override_native_assertion_fail(
    monkeypatch, fake_provider_credentials
):
    result = await _run_pro_task(monkeypatch, {"test_summary": FORGED_PASS}, NATIVE_FAIL)
    assert native_assertion_exit_code(result) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("run_outcome", [None, {"task_status": "completed", "tests": {}}])
async def test_model_forged_pass_without_native_evidence_is_not_success(
    monkeypatch, fake_provider_credentials, run_outcome
):
    result = await _run_pro_task(monkeypatch, {"test_summary": FORGED_PASS}, run_outcome)
    assert native_assertion_exit_code(result) not in (0, 1)
