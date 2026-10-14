from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage
import pytest

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.validator.tool_declarations import REPORT_TASK_STATUS_TOOL
from artemis.context import ArtemisContext
from artemis.graph.state import State


@pytest.fixture
def runner(tmp_path, fake_provider_credentials):
    context = Mock(spec=ArtemisContext)
    context.llm_config = Mock()
    context.llm_config.get_agent.return_value = SimpleNamespace(
        model="gemini-2.5-flash", provider="google", temperature=0.1
    )
    context.device = SimpleNamespace(device_width=1080, device_height=2400)
    context.adb_client = None
    context.driver = Mock()
    context.data_engine = Mock()
    context.data_engine.base_dir = tmp_path
    context.data_engine.current_step_id = None
    context.data_engine.get_step_record.return_value = None
    context.data_engine.get_step_image_path.return_value = None
    with patch("artemis.controllers.unified_controller.get_driver"):
        result = FlashRunner(context, goal="Test the app", max_turns=1)
    result.summarizer = None
    result._init_llm = Mock()
    result._get_tools = Mock(return_value=[REPORT_TASK_STATUS_TOOL])
    result._read_injected_instruction = AsyncMock(return_value=(None, None))
    return result


async def run_report(runner, arguments):
    runner._invoke_model = AsyncMock(
        return_value=AIMessage(
            content="Report",
            tool_calls=[{"id": "report", "name": "report_task_status", "args": arguments}],
        )
    )
    with patch(
        "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
        AsyncMock(return_value=(None, b"image", [])),
    ):
        return await runner.run(State(initial_goal="Test the app"))


def test_report_contract_explains_each_cause():
    properties = REPORT_TASK_STATUS_TOOL.parameters["properties"]
    assert properties["status"]["enum"] == ["completed", "failed"]
    assert properties["cause"]["enum"] == [
        "app_defect",
        "goal_unreachable",
        "blocked_by_environment",
    ]
    assert {"evidence_step", "expected", "actual"} <= properties.keys()
    for cause in properties["cause"]["enum"]:
        assert cause in properties["cause"]["description"]
    assert "cause" not in REPORT_TASK_STATUS_TOOL.parameters["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["app_defect", "goal_unreachable", "blocked_by_environment"])
async def test_fake_model_reports_each_cause(runner, cause):
    result = await run_report(
        runner,
        {
            "status": "failed",
            "cause": cause,
            "explanation": "Observed failure",
            "expected": "Settings opens",
            "actual": "Settings does not open",
        },
    )
    assert result["cause"] == cause
    assert result["expected"] == "Settings opens"
    assert result["actual"] == "Settings does not open"
    assert "evidence_ref" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("cause", "typo"),
        ("cause", ["app_defect"]),
        ("cause", 1),
        ("status", "success"),
        ("status", ["completed"]),
    ],
)
async def test_invalid_report_values_are_missing_and_logged(runner, field, value):
    with patch("artemis.agents.flash.runner.logger") as log:
        result = await run_report(runner, {"status": "failed", field: value})
    assert field not in result
    assert "report_cause_invalid" in str(log.warning.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("citation", [None, 99, True, "../other-run", 1])
async def test_app_defect_never_borrows_a_nearby_screenshot(runner, citation):
    if citation == 1:
        runner.ctx.data_engine.get_step_record.return_value = SimpleNamespace(step_id=uuid4())
    result = await run_report(
        runner,
        {
            "status": "failed",
            "cause": "app_defect",
            "evidence_step": citation,
            "evidence_ref": {"step_id": "model-forged"},
        },
    )
    assert "evidence_ref" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["pre", "post"])
async def test_app_defect_resolves_only_the_cited_screenshot(runner, tmp_path, which):
    step_id = uuid4()
    image = tmp_path / "image.jpg"
    image.write_bytes(b"screenshot")
    runner.ctx.data_engine.get_step_record.return_value = SimpleNamespace(
        step_id=step_id, step_number=3, pre_image_name="before", post_image_name="after"
    )
    runner.ctx.data_engine.get_step_image_path.side_effect = lambda step_number, position: (
        image if position == which else None
    )
    result = await run_report(
        runner,
        {
            "status": "failed",
            "cause": "app_defect",
            "evidence_step": 3,
        },
    )
    assert result["evidence_ref"] == {
        "step_id": str(step_id),
        "step_number": 3,
        "image_name": "after" if which == "post" else "before",
        "which": which,
    }
    runner.ctx.data_engine.get_step_record.assert_called_once_with(3)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, AIMessage(content="No tool call")])
async def test_runner_stops_without_inventing_a_cause(runner, response):
    runner._invoke_model = AsyncMock(return_value=response)
    with patch(
        "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
        AsyncMock(return_value=(None, b"image", [])),
    ):
        result = await runner.run(State(initial_goal="Test the app"))
    assert result["status"] == "failed"
    assert "cause" not in result


@pytest.mark.asyncio
async def test_completed_report_references_its_final_screenshot(runner, tmp_path):
    step_id = uuid4()
    image = tmp_path / "final.jpg"
    image.write_bytes(b"screenshot")
    runner.ctx.data_engine.record_step.return_value = step_id
    runner.ctx.data_engine.get_step_number.return_value = 4
    runner.ctx.data_engine.get_step_record.return_value = SimpleNamespace(
        step_id=step_id,
        step_number=4,
        pre_image_name="final",
        post_image_name=None,
    )
    runner.ctx.data_engine.get_step_image_path.side_effect = lambda step_number, which: (
        image if which == "pre" else None
    )
    result = await run_report(runner, {"status": "completed"})
    assert result["evidence_ref"] == {
        "step_id": str(step_id),
        "step_number": 4,
        "image_name": "final",
        "which": "pre",
    }
