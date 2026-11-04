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


# -- every device operation keeps the typed disconnect ----------------------


def _driver(shell_side_effect, ui_client=...):
    from unittest.mock import Mock

    from artemis.drivers.android.adb_driver import AndroidAdbDriver

    driver = AndroidAdbDriver("R58M123", Mock() if ui_client is ... else ui_client)
    driver._device = Mock()
    driver._device.shell.side_effect = shell_side_effect
    return driver


_LOST = AdbError("device 'R58M123' not found")
_OPERATIONS = {
    "swipe": lambda d: d.swipe(1, 2, 3, 4),
    "swipe_direction": lambda d: d.swipe_direction("up"),
    "input_text_clear": lambda d: d.input_text("hi", clear_existing=True),
    "input_text_append": lambda d: d.input_text("hi", clear_existing=False),
    "press_key": lambda d: d.press_key("back"),
    "launch_app": lambda d: d.launch_app("com.example"),
    "stop_app": lambda d: d.stop_app("com.example"),
    "long_press": lambda d: d.long_press(1, 2),
}


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
@pytest.mark.asyncio
async def test_every_driver_operation_raises_the_typed_disconnect(operation):
    driver = _driver(_LOST)
    driver._width, driver._height = 1080, 2400

    with pytest.raises(DeviceDisconnectedError) as raised:
        await _OPERATIONS[operation](driver)

    assert raised.value.reason == "not found"


@pytest.mark.parametrize("operation", sorted(_OPERATIONS))
@pytest.mark.asyncio
async def test_ordinary_adb_errors_stay_a_plain_failure(operation):
    driver = _driver(AdbError("permission denied"))
    driver._width, driver._height = 1080, 2400

    assert await _OPERATIONS[operation](driver) is False


@pytest.mark.asyncio
async def test_input_text_paste_tier_does_not_swallow_a_disconnect():
    from unittest.mock import Mock

    ui_client = Mock()
    ui_client.set_clipboard.return_value = True
    # the clear succeeds, then the paste keyevent finds the phone gone
    driver = _driver([None, _LOST], ui_client=ui_client)

    with pytest.raises(DeviceDisconnectedError):
        await driver.input_text("hi")
    assert driver._device.shell.call_count == 2  # no ADBKeyboard / input-text fallback


@pytest.mark.asyncio
async def test_input_text_adbkeyboard_tier_does_not_swallow_a_disconnect():
    driver = _driver([None, "com.android.adbkeyboard/.AdbIME", _LOST], ui_client=None)

    with pytest.raises(DeviceDisconnectedError):
        await driver.input_text("hi")
    assert driver._device.shell.call_count == 3  # no native input-text fallback


def _android_runner(shell_side_effect):
    from unittest.mock import Mock

    from artemis.controllers.unified_controller import UnifiedMobileController
    from artemis.drivers.android.adb_driver import AndroidAdbDriver
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.actuators.adb import AdbActuator

    serial = "R58M123"
    driver = AndroidAdbDriver(serial, Mock())
    driver._device = Mock()
    driver._device.shell.side_effect = shell_side_effect
    driver._width, driver._height = 1080, 2400
    context = SimpleNamespace(
        device=SimpleNamespace(device_id=serial, device_width=1080, device_height=2400),
        data_engine=None,
    )
    controller = UnifiedMobileController.__new__(UnifiedMobileController)
    controller.ctx = context
    controller._driver = driver
    actuator = AdbActuator(context, controller)
    executor = McpActionExecutor(context, actuator=actuator)

    async def call_action(name, args):
        if name == "swipe":
            return await actuator.swipe(tuple(args["start"]), tuple(args["end"]))
        if name == "press_key":
            return await actuator.press_key(args["key"])
        if name == "input_text":
            return await actuator.input_text(args["text"], None, args.get("clear_exist", True))
        raise AssertionError(f"unexpected wire call {name}")

    executor._session = SimpleNamespace(started=True, call=call_action)
    runner = FlashRunner.__new__(FlashRunner)
    runner.ctx = context
    runner.controller = controller
    runner.executor = executor
    runner.summarizer = None
    runner.goal = "g"
    return runner, driver


@pytest.mark.parametrize(
    "first_call",
    [
        {"id": "a", "name": "swipe", "args": {"direction": "up"}},
        {"id": "a", "name": "press_key", "args": {"key": "back"}},
        {"id": "a", "name": "input_text", "args": {"text": "hello"}},
    ],
    ids=["swipe", "press_key", "input_text"],
)
@pytest.mark.asyncio
async def test_a_lost_device_during_any_action_stops_the_run_with_no_retry_or_fallback(
    first_call, monkeypatch
):
    runner, driver = _android_runner(_LOST)
    fallback = AsyncMock(side_effect=OSError("screenshot unavailable"))
    monkeypatch.setattr("artemis.agents.flash.runner.observe", fallback)
    second = {"id": "b", "name": "press_key", "args": {"key": "home"}}

    with patch("artemis.agents.flash.runner.tool_result_messages", return_value=[]):
        report, *_ = await runner._process_tool_calls(
            [first_call, second],
            SimpleNamespace(indexed_elements=[]),
            [],
            "",
            {},
            None,
            None,
            0,
            _TurnRecord(),
        )

    assert (report["status"], report["interrupt_reason"]) == ("interrupted", "device_offline")
    assert driver._device.shell.call_count == 1  # the second action never ran
    assert fallback.await_count == 0


# -- the disconnect survives telemetry failures ------------------------------


@pytest.mark.parametrize("failing", ["_record_action_step", "_build_action_record"])
@pytest.mark.asyncio
async def test_disconnect_latches_even_when_recording_raises(failing):
    execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="first",
            tool_name="tap",
            status="error",
            text_summary="Error during tap: device 'R58M123' not found",
        )
    )
    runner = _runner(execute)
    runner.ctx.data_engine = object()  # step recording enabled
    runner._build_action_record = lambda *a, **k: {}

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    setattr(runner, failing, explode)

    report = await _process(runner)

    assert report["status"] == "interrupted"
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_disconnect_latches_when_message_construction_raises():
    execute = AsyncMock(
        return_value=ToolExecutionResult(
            tool_call_id="first",
            tool_name="tap",
            status="error",
            text_summary="Error during tap: device 'R58M123' not found",
        )
    )
    runner = _runner(execute)
    tool_calls = [
        {"id": "first", "name": "tap", "args": {"target": [1, 2]}},
        {"id": "second", "name": "tap", "args": {"target": [3, 4]}},
    ]

    with patch(
        "artemis.agents.flash.runner.tool_result_messages", side_effect=ValueError("bad block")
    ):
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

    assert report["status"] == "interrupted"
    assert execute.await_count == 1
