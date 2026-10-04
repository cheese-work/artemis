"""The flash worker stops on the first adb device loss and reports it as an interruption (CHE-1089)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from adbutils import AdbError
import pytest

from artemis.agents.flash.runner import FlashRunner, _TurnRecord
from artemis.agents.validator.tool_declarations import ToolExecutionResult
from artemis.drivers.types import DeviceDisconnectedError, device_disconnect_reason

pytestmark = pytest.mark.usefixtures("fake_provider_credentials")


@pytest.mark.parametrize(
    ("serial", "message", "expected"),
    [
        ("R58M123", "Error during tap: device 'R58M123' not found", "not found"),
        ("127.0.0.1:35409", "device '127.0.0.1:35409' not found", "not found"),
        ("R58M123", "error: device offline", "offline"),
        ("R58M123", "device 'R58M123' offline", "offline"),
        (None, "device 'emulator-5554' not found", "not found"),
        # another phone's loss, or an unrelated "not found", is not this run's disconnect
        ("R58M123", "device 'OTHER' not found", None),
        ("R58M123", "Error during tap: UI element not found", None),
        ("R58M123", "package com.example not found", None),
        ("R58M123", "", None),
    ],
)
def test_device_disconnect_reason(serial, message, expected):
    assert device_disconnect_reason(serial, message) == expected


def _runner(execute) -> FlashRunner:
    runner = FlashRunner.__new__(FlashRunner)
    runner.executor = SimpleNamespace(action_tool_names=frozenset({"tap"}), execute=execute)
    runner.ctx = SimpleNamespace(device=SimpleNamespace(device_id="R58M123"), data_engine=None)
    runner.summarizer = None
    runner.goal = "g"
    return runner


async def _process(runner):
    tool_calls = [
        {"id": "first", "name": "tap", "args": {"target": [1, 2]}},
        {"id": "second", "name": "tap", "args": {"target": [3, 4]}},
    ]
    with patch("artemis.agents.flash.runner.tool_result_messages", return_value=[]):
        report, *_ = await runner._process_tool_calls(
            tool_calls,
            SimpleNamespace(indexed_elements=[]),
            [],
            "",
            {},
            None,
            None,
            0,
            _TurnRecord(),
        )
    return report


@pytest.mark.parametrize("message", ["device 'R58M123' not found", "error: device offline"])
@pytest.mark.asyncio
async def test_error_result_with_device_loss_stops_the_run_without_retry(message):
    execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="first",
            tool_name="tap",
            status="error",
            text_summary=f"Error during tap: {message}",
        )
    )
    runner = _runner(execute)

    report = await _process(runner)

    assert report["status"] == "interrupted"
    assert report["interrupt_reason"] == "device_offline"
    assert "disconnected" in report["explanation"].lower()
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_raised_device_disconnected_error_stops_the_run_without_retry():
    execute = AsyncMock(side_effect=DeviceDisconnectedError("R58M123", "offline"))
    runner = _runner(execute)

    report = await _process(runner)

    assert (report["status"], report["interrupt_reason"]) == ("interrupted", "device_offline")
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_ordinary_tool_failure_keeps_the_run_going():
    execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="first",
            tool_name="tap",
            status="error",
            text_summary="Error during tap: UI element not found",
        )
    )
    runner = _runner(execute)
    runner._capture_post_screenshot = AsyncMock(return_value=None)

    report = await _process(runner)

    assert report is None
    assert execute.await_count == 2


@pytest.mark.asyncio
async def test_driver_tap_raises_device_disconnected_for_a_lost_device():
    from unittest.mock import Mock

    from artemis.drivers.android.adb_driver import AndroidAdbDriver

    driver = AndroidAdbDriver("R58M123", Mock())
    driver._device = Mock()
    driver._device.shell.side_effect = AdbError("device 'R58M123' not found")

    with pytest.raises(DeviceDisconnectedError) as raised:
        await driver.tap(10, 20)
    assert raised.value.reason == "not found"

    driver._device.shell.side_effect = AdbError("permission denied")
    assert await driver.tap(10, 20) is False
