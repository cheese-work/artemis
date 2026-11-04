"""Opt-in native assertion-exit contract (CHE-3645) -- mocked Agent/device boundary only."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click import unstyle
from typer.testing import CliRunner

from artemis.interfaces.cli.assertion_exit import native_assertion_exit_code
from artemis.interfaces.cli.commands import run as run_module
from artemis.interfaces.cli.main import app
from artemis.sdk.agent import attach_test_summary

from tests.unit.cli.test_run_command_gate1 import _fake_llm_config

SID = "assertion-exit-sid"
ARGS = {
    "goal": "host-only",
    "session_id": SID,
    "device_serial": "emulator-5554",
    "locked_app_package": "ai.multica.mobile",
    "assertion_exit": True,
}
FAILED_ITEM = {"kind": "assert", "item_text": "x is visible", "evidence": "x not on screen"}


@pytest.fixture(autouse=True)
def _host_key(monkeypatch):
    monkeypatch.setattr(run_module.settings, "GOOGLE_API_KEY", "fake-test-key")


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


FAILED = summary(passed=0, failed=1, failed_items=[FAILED_ITEM])


def fake_agent(result=None, run_error=None, clean_error=None):
    agent = MagicMock()
    agent.init = AsyncMock()
    agent.run_task = AsyncMock(return_value=result, side_effect=run_error)
    agent.clean = AsyncMock(side_effect=clean_error)
    return agent


def patched(agent):
    reconcile = MagicMock()
    ctx = (
        patch.object(run_module, "initialize_llm_config", return_value=_fake_llm_config()),
        patch.object(run_module, "Agent", return_value=agent),
        patch.object(run_module, "record_attempt_manifest"),
        patch.object(run_module, "reconcile_and_store_verdict", reconcile),
    )
    return ctx, reconcile


async def run(agent, **overrides):
    ctx, reconcile = patched(agent)
    for c in ctx:
        c.start()
    try:
        await run_module.execute_task(**{**ARGS, **overrides})
    finally:
        patch.stopall()
    return reconcile


def test_native_pass_exits_zero():
    assert native_assertion_exit_code(summary()) == 0


def test_completed_native_assertion_fail_exits_one():
    assert native_assertion_exit_code(FAILED) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"task_status": "blocked"},
        {"passed": 0},  # zero checks
        {"unchecked": 1},
        {"inconclusive": 1},
        {"retired": 1},
        {"retired_items": [{"item_text": "r"}]},
        {"passed": "1"},
        {"passed": True},
        {"failed": -1},
        {"failed": 1},  # counter without matching item evidence
        {"failed": 1, "failed_items": [{"kind": "assert", "item_text": "x", "evidence": " "}]},
        {"failed_items": "no"},
    ],
)
def test_incomplete_or_malformed_evidence_is_not_success(updates):
    assert native_assertion_exit_code(summary(**updates)) not in (0, 1)


@pytest.mark.parametrize("result", [None, "completed", {}, {"test_summary": None}])
def test_missing_evidence_is_not_success(result):
    assert native_assertion_exit_code(result) not in (0, 1)


@pytest.mark.parametrize("outcome", [None, {"task_status": "completed", "tests": {}}])
def test_model_supplied_summary_cannot_supply_pass(outcome):
    spoofed = attach_test_summary(summary(), outcome)
    assert native_assertion_exit_code(spoofed) not in (0, 1)


def test_native_summary_replaces_model_supplied_one():
    out = attach_test_summary(
        summary(), {"task_status": "completed", "tests": FAILED["test_summary"]}
    )
    assert native_assertion_exit_code(out) == 1


@pytest.mark.asyncio
async def test_pass_returns_cleanly_with_run_identity():
    agent = fake_agent(summary())
    reconcile = await run(agent)
    agent.clean.assert_awaited_once()
    assert reconcile.call_args.kwargs["trace_id"] == SID


@pytest.mark.asyncio
async def test_assertion_fail_exits_one_with_identity_and_cleanup():
    agent = fake_agent(FAILED)
    ctx, reconcile = patched(agent)
    with ctx[0], ctx[1], ctx[2], ctx[3], pytest.raises(SystemExit) as raised:
        await run_module.execute_task(**ARGS)
    assert raised.value.code == 1
    agent.clean.assert_awaited_once()
    assert reconcile.call_args.kwargs["trace_id"] == SID


@pytest.mark.asyncio
async def test_missing_result_exits_non_success():
    agent = fake_agent(None)
    ctx, _ = patched(agent)
    with ctx[0], ctx[1], ctx[2], ctx[3], pytest.raises(SystemExit) as raised:
        await run_module.execute_task(**ARGS)
    assert raised.value.code not in (0, 1)


@pytest.mark.asyncio
async def test_cleanup_error_after_nominal_pass_is_not_clean_pass():
    agent = fake_agent(summary(), clean_error=RuntimeError("cleanup failed"))
    ctx, reconcile = patched(agent)
    with ctx[0], ctx[1], ctx[2], ctx[3], pytest.raises(RuntimeError, match="cleanup failed"):
        await run_module.execute_task(**ARGS)
    assert reconcile.call_args.kwargs["trace_id"] == SID


@pytest.mark.asyncio
async def test_cancellation_keeps_identity_and_cleans():
    agent = fake_agent(run_error=asyncio.CancelledError())
    ctx, reconcile = patched(agent)
    with ctx[0], ctx[1], ctx[2], ctx[3], pytest.raises(asyncio.CancelledError):
        await run_module.execute_task(**ARGS)
    agent.clean.assert_awaited_once()
    assert reconcile.call_args.kwargs["trace_id"] == SID


@pytest.mark.asyncio
async def test_default_path_swallows_cleanup_error_and_ignores_result():
    agent = fake_agent(FAILED, clean_error=RuntimeError("cleanup failed"))
    ctx, _ = patched(agent)
    args = {k: v for k, v in ARGS.items() if k != "assertion_exit"}
    with ctx[0], ctx[1], ctx[2], ctx[3]:
        await run_module.execute_task(**args)  # unchanged: no exit, no raise


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["device_serial", "locked_app_package"])
@pytest.mark.parametrize("value", [None, "", "  "])
async def test_requires_exact_package_and_serial_before_agent(missing, value):
    with patch.object(run_module, "Agent") as agent:
        with pytest.raises(ValueError, match="assertion-exit"):
            await run_module.execute_task(**{**ARGS, missing: value})
    agent.assert_not_called()


@pytest.mark.parametrize(
    "argv,env",
    [
        ([], {}),  # daemon route
        (["--standalone"], {"ARTEMIS_TASK_WORKER": "1"}),  # worker
    ],
)
def test_unsupported_invocation_rejected_before_execute(argv, env):
    with (
        patch.object(run_module, "execute_task", new_callable=AsyncMock) as execute,
        patch.object(run_module, "Agent") as agent,
    ):
        result = CliRunner().invoke(
            app,
            [
                "run",
                "goal",
                "--assertion-exit",
                "-s",
                "emulator-5554",
                "-a",
                "ai.multica.mobile",
                *argv,
            ],
            env=env,
        )
    assert result.exit_code != 0
    assert "--standalone" in unstyle(result.output)
    execute.assert_not_called()
    agent.assert_not_called()


@pytest.mark.parametrize("result,code", [(summary(), 0), (FAILED, 1)])
def test_standalone_cli_exit_code_follows_native_result(result, code):
    agent = fake_agent(result)
    ctx, _ = patched(agent)
    with ctx[0], ctx[1], ctx[2], ctx[3], patch.object(run_module, "display_device_status"):
        out = CliRunner().invoke(
            app,
            [
                "run",
                "goal",
                "--standalone",
                "--assertion-exit",
                "-s",
                "emulator-5554",
                "-a",
                "ai.multica.mobile",
            ],
        )
    assert out.exit_code == code
    agent.clean.assert_awaited_once()
