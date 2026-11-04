import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click import unstyle
from typer.testing import CliRunner

from artemis.config.agent import AgentGlobalConfig, load_agent_config
from artemis.config.llm import LLMConfig, _expand_default_into_nodes
from artemis.interfaces.cli import qualification as qualification_module
from artemis.interfaces.cli.commands import run as run_module
from artemis.interfaces.cli.main import app
from artemis.interfaces.cli.qualification import d1_exit_code, prepare_d1_config
from artemis.sdk.agent import attach_test_summary
from artemis.sdk.agent import Agent as NativeAgent
from artemis.sdk.types.task import AgentProfile


@pytest.fixture(autouse=True)
def isolate_host_run(monkeypatch):
    monkeypatch.setattr(run_module, "publish_startup_progress", MagicMock())
    monkeypatch.setattr(run_module.settings, "OPENAI_API_KEY", "host-test-not-a-key")
    monkeypatch.setattr(qualification_module, "load_agent_config", approved_agent_config)


def approved_agent_config():
    return AgentGlobalConfig.model_validate(
        {
            "flash": {"step_summarizer": {"model": "gpt-6.1-sol"}},
            "memory": {"chunking": {"model": "gpt-6.1-sol"}},
        }
    )


def model_config():
    return LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                    "fallback": {"provider": "openai", "model": "gpt-6.1-sol"},
                }
            }
        )
    )


def summary(**updates):
    result = {
        "task_status": "completed",
        "passed": 1,
        "failed": 0,
        "inconclusive": 0,
        "unchecked": 0,
        "retired": 0,
        "failed_items": [],
        "retired_items": [],
    }
    result.update(updates)
    return {"test_summary": result}


def test_d1_config_pins_optional_judges_without_mutating_loaded_config():
    original = model_config()
    selected = prepare_d1_config(original)
    assert original.planner_validation is None
    for name in ("planner_validation", "validator_pixel_safety_net"):
        assert selected.get_agent(name).provider == "openai"
        assert selected.get_agent(name).model == "gpt-6.1-sol"
        assert selected.get_agent(name).fallback.model == "gpt-6.1-sol"


def test_published_d1_config_selects_approved_route_without_fallback_retries():
    config_path = (
        Path(__file__).resolve().parents[3] / "qualification/configs/che984-d1.artemis.jsonc"
    )
    configured = LLMConfig.model_validate(
        _expand_default_into_nodes(json.loads(config_path.read_text()))
    )
    selected = prepare_d1_config(configured)
    assert selected.checker.model == "gpt-6.1-sol"
    assert selected.checker.fallback_enabled is False


@pytest.mark.parametrize("path", ["flash.step_summarizer", "memory.chunking"])
def test_d1_rejects_unapproved_memory_model_overrides(monkeypatch, path):
    agent_config = approved_agent_config()
    section, name = path.split(".")
    getattr(getattr(agent_config, section), name).model = "gemini-3.8-flash"
    monkeypatch.setattr(qualification_module, "load_agent_config", lambda: agent_config)
    with pytest.raises(ValueError, match="gpt-6.1-sol"):
        prepare_d1_config(model_config())


@pytest.mark.asyncio
async def test_d1_rejects_default_memory_models_before_agent_initialization(monkeypatch):
    monkeypatch.setattr(qualification_module, "load_agent_config", AgentGlobalConfig)
    native_agent = MagicMock()
    native_agent.init = AsyncMock()
    native_agent.run_task = AsyncMock(return_value=summary())
    native_agent.clean = AsyncMock()
    with (
        patch.object(run_module, "initialize_llm_config", return_value=model_config()),
        patch.object(run_module, "Agent", return_value=native_agent) as agent,
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict"),
    ):
        with pytest.raises(ValueError, match="gpt-6.1-sol"):
            await run_module.execute_task(
                goal="host-only configuration validation",
                device_serial="emulator-5554",
                profile="pro",
                locked_app_package="com.example.synthetic.d1offline",
                qualification="che984-d1",
            )
    agent.assert_not_called()


def test_published_d1_config_pins_native_memory_model_selection(monkeypatch):
    from artemis.memory import ensure_step_memory
    from artemis.memory.chunking import StepCapsuleLens

    config_path = (
        Path(__file__).resolve().parents[3] / "qualification/configs/che984-d1.artemis.jsonc"
    )
    agent_config = load_agent_config(config_path)
    monkeypatch.setattr(qualification_module, "load_agent_config", lambda: agent_config)
    configured = LLMConfig.model_validate(
        _expand_default_into_nodes(json.loads(config_path.read_text()))
    )
    context = SimpleNamespace(llm_config=prepare_d1_config(configured), data_engine=None)
    with (
        patch("artemis.config.load_agent_config", return_value=agent_config),
        patch("artemis.llm.router.ModelFactory.get_model", return_value=MagicMock()) as factory,
    ):
        ensure_step_memory(context)
        StepCapsuleLens(agent_config.memory.chunking.model, ctx=context)._get_llm()
    assert factory.call_count == 2
    for call in factory.call_args_list:
        assert call.args[0].provider == "openai"
        assert call.args[0].model_name == "gpt-6.1-sol"


@pytest.mark.parametrize("field", ["model", "provider", "fix_model", "fallback"])
def test_d1_config_rejects_unapproved_routes(field):
    config = model_config()
    update = {field: "other"}
    if field == "fallback":
        update = {field: config.checker.fallback.model_copy(update={"model": "other"})}
    config.checker = config.checker.model_copy(update=update)
    with pytest.raises(ValueError, match="gpt-6.1-sol"):
        prepare_d1_config(config)


def test_d1_clean_summary_has_success_exit():
    assert d1_exit_code(summary()) == 0


def test_d1_failed_assertion_has_failing_exit_despite_completed_status():
    failed = summary(
        passed=0,
        failed=1,
        failed_items=[
            {
                "kind": "assert",
                "item_text": "D1-NC-01 attached=true",
                "evidence": "Observed attached=false",
            }
        ],
    )
    assert d1_exit_code(failed) == 1
    assert (
        attach_test_summary(
            {"test_summary": summary()["test_summary"]},
            {"task_status": "completed", "tests": failed["test_summary"]},
        )["test_summary"]["failed"]
        == 1
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"task_status": "blocked"},
        {"unchecked": 1},
        {"inconclusive": 1},
        {"retired": 1},
        {"passed": 0},
        {"failed": -1},
        {"passed": True},
        {"passed": "1"},
        {"failed": 1},
        {"failed_items": "not a list"},
    ],
)
def test_d1_incomplete_or_malformed_summary_is_not_success(updates):
    assert d1_exit_code(summary(**updates)) == 2


@pytest.mark.parametrize("result", [None, "completed", {}, {"test_summary": None}])
def test_d1_missing_summary_is_not_success(result):
    assert d1_exit_code(result) == 2


@pytest.mark.parametrize("outcome", [None, {"task_status": "completed", "tests": {}}])
def test_model_output_cannot_fabricate_a_native_summary(outcome):
    assert d1_exit_code(attach_test_summary(summary(), outcome)) == 2


def test_d1_cannot_silently_route_through_daemon():
    with patch.object(run_module, "execute_task", new_callable=AsyncMock) as execute:
        result = CliRunner().invoke(app, ["run", "goal", "--qualification", "che984-d1"])
    assert result.exit_code != 0
    assert "--standalone" in unstyle(result.output)
    execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,exit_code",
    [
        (summary(), 0),
        (None, 2),
        (
            summary(
                passed=0,
                failed=1,
                failed_items=[
                    {
                        "kind": "assert",
                        "item_text": "D1-NC-01 attached=true",
                        "evidence": "attached=false",
                    }
                ],
            ),
            1,
        ),
    ],
)
async def test_d1_native_execution_maps_result_and_cleans(monkeypatch, result, exit_code):
    monkeypatch.delenv("ARTEMIS_CHAT_SESSION_ID", raising=False)
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock(return_value=result)
    agent.clean = AsyncMock()
    with (
        patch.object(run_module, "initialize_llm_config", return_value=model_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict"),
    ):
        invocation = run_module.execute_task(
            goal="host-only result plumbing",
            device_serial="emulator-5554",
            profile="pro",
            locked_app_package="com.example.synthetic.d1offline",
            qualification="che984-d1",
        )
        if exit_code:
            with pytest.raises(SystemExit) as raised:
                await invocation
            assert raised.value.code == exit_code
        else:
            await invocation
    agent.clean.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"profile": "flash"},
        {"device_serial": None},
        {"device_serial": "phone"},
        {"enable_checker": False},
        {"verification_level": "off"},
        {"locked_app_package": "other"},
    ],
)
async def test_d1_bad_invocation_stops_before_agent_or_device(options):
    arguments = {
        "goal": "host-only result plumbing",
        "device_serial": "emulator-5554",
        "profile": "pro",
        "locked_app_package": "com.example.synthetic.d1offline",
        "qualification": "che984-d1",
        **options,
    }
    with patch.object(run_module, "Agent") as agent:
        with pytest.raises(ValueError, match="CHE-984 D1"):
            await run_module.execute_task(**arguments)
    agent.assert_not_called()


@pytest.mark.asyncio
async def test_d1_cleanup_failure_cannot_report_success():
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock(return_value=summary())
    agent.clean = AsyncMock(side_effect=RuntimeError("cleanup failed"))
    with (
        patch.object(run_module, "initialize_llm_config", return_value=model_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict"),
    ):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await run_module.execute_task(
                goal="host-only result plumbing",
                device_serial="emulator-5554",
                profile="pro",
                locked_app_package="com.example.synthetic.d1offline",
                qualification="che984-d1",
            )


@pytest.mark.parametrize(
    "result,expected_exit",
    [
        (summary(), 0),
        (None, 2),
        (
            summary(
                passed=0,
                failed=1,
                failed_items=[
                    {
                        "kind": "assert",
                        "item_text": "D1-NC-01 attached=true",
                        "evidence": "Observed attached=false",
                    }
                ],
            ),
            1,
        ),
    ],
)
def test_standalone_cli_exits_from_native_result_without_device_probe(result, expected_exit):
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock(return_value=result)
    agent.clean = AsyncMock()
    with (
        patch.object(run_module, "initialize_llm_config", return_value=model_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict"),
        patch.object(run_module, "display_device_status") as device_probe,
    ):
        invocation = CliRunner().invoke(
            app,
            [
                "run",
                "host-only result plumbing",
                "--standalone",
                "--qualification",
                "che984-d1",
                "--device-serial",
                "emulator-5554",
                "--locked-app",
                "com.example.synthetic.d1offline",
            ],
        )
    assert invocation.exit_code == expected_exit
    assert f"CHE984_D1_ASSERTION_EXIT={expected_exit}" in invocation.output
    agent.clean.assert_awaited_once()
    device_probe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["ARTEMIS_FAKE_LLM", "ARTEMIS_MOCK_DRIVER", "ARTEMIS_CLOUD_MODE", "ARTEMIS_CHAT_SESSION_ID"],
)
async def test_d1_cannot_replace_real_local_native_route(monkeypatch, mode):
    monkeypatch.setenv(mode, "1")
    with patch.object(run_module, "Agent") as agent:
        with pytest.raises(ValueError, match="CHE-984 D1"):
            await run_module.execute_task(
                goal="host-only result plumbing",
                device_serial="emulator-5554",
                profile="pro",
                locked_app_package="com.example.synthetic.d1offline",
                qualification="che984-d1",
            )
    agent.assert_not_called()


@pytest.mark.asyncio
async def test_d1_real_agent_init_never_prewarms_google_clients(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", SecretStr("host-test-not-a-key"))
    config = (
        run_module.Builders.AgentConfig.with_default_profile(
            AgentProfile(name="d1-host-test", llm_config=prepare_d1_config(model_config()))
        )
        .for_device("android", "emulator-5554")
        .build()
    )
    agent = NativeAgent(config=config, session_id="d1-host-init")
    with (
        patch.object(run_module, "initialize_llm_config", return_value=model_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict"),
        patch.object(agent, "_init_clients"),
        patch.object(
            agent, "_get_device_context", new_callable=AsyncMock, return_value=MagicMock()
        ),
        patch.object(agent, "run_task", new_callable=AsyncMock, return_value=summary()),
        patch.object(agent, "clean", new_callable=AsyncMock),
        patch("artemis.sdk.agent.EndpointTransport.adb_binary", return_value="host-test-adb"),
        patch("artemis.sdk.agent.publish_startup_progress"),
        patch("artemis.sdk.agent.genai.Client") as native_google,
        patch("artemis.sdk.agent.ChatGoogleGenerativeAI") as langchain_google,
        patch.object(agent, "_prewarm_llm_connections", new_callable=AsyncMock) as prewarm,
    ):
        await run_module.execute_task(
            goal="host-only initialization plumbing",
            device_serial="emulator-5554",
            profile="pro",
            locked_app_package="com.example.synthetic.d1offline",
            qualification="che984-d1",
        )
    assert agent._initialized is True
    prewarm.assert_not_called()
    native_google.assert_not_called()
    langchain_google.assert_not_called()
