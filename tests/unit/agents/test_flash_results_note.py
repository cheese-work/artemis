"""Flash can write notes: the results.md ledger that the chat UI shows per turn."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import AIMessage
import pytest

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.operator.prompts import apply_operator_prompt_contract, load_operator_prompts
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.mcp.action_executor import McpActionExecutor

pytestmark = pytest.mark.usefixtures("fake_provider_credentials")

# Representative of the 5-session VPN prompt (13/10 16:42); the original text is not in the repo.
VPN_PROMPT = (
    "Mở app VPN, vào List Server và đếm số server. Lặp lại cho 5 phiên: "
    "phiên 1 kết nối server đầu, phiên 2 server thứ hai, phiên 3 server thứ ba, "
    "phiên 4 server thứ tư, phiên 5 server thứ năm."
)
ROWS = [f"| Phiên {n} | NOT TESTED | | |" for n in range(1, 6)]
TABLE = "\n".join(
    [
        "| Mục | Trạng thái | Kết quả | Bằng chứng |",
        "|---|---|---|---|",
        *ROWS,
        "TOTAL: Planned 5 / PASS 0 / FAIL 0 / BLOCKED 0 / NOT TESTED 5",
    ]
)


def _context(base_dir: Path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    ctx.llm_config.get_agent.return_value = SimpleNamespace(
        model="gemini-2.5-flash", provider="google", temperature=0.1
    )
    ctx.device = SimpleNamespace(device_width=1080, device_height=2400)
    ctx.adb_client = None
    ctx.driver = Mock()
    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = base_dir
    ctx.data_engine.current_step_id = None
    ctx.data_engine.wait_for_pending_operations = AsyncMock()
    ctx.data_engine.get_step_record.return_value = None
    ctx.data_engine.get_step_image_path.return_value = None
    return ctx


def _actuator():
    actuator = Mock()
    actuator.extensions.return_value = []
    return actuator


@pytest.fixture
def runner(tmp_path):
    with patch("artemis.controllers.unified_controller.get_driver"):
        result = FlashRunner(_context(tmp_path), goal=VPN_PROMPT, max_turns=5)
    result.summarizer = None
    result._init_llm = Mock()
    result._read_injected_instruction = AsyncMock(return_value=(None, None))
    return result


def test_flash_declares_all_four_note_tools(runner):
    names = {tool.name for tool in runner._get_tools()}
    assert {"save_note", "update_note", "read_note", "list_notes"} <= names


def test_flash_prompt_teaches_results_ledger(runner):
    prompt = runner._render_system_prompt(runner._get_tools())
    assert "results" in prompt
    assert "| Mục | Trạng thái | Kết quả | Bằng chứng |" in prompt
    assert "TOTAL: Planned" in prompt


def test_pro_prompt_teaches_results_ledger():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["main_template"])
    assert "| Mục | Trạng thái | Kết quả | Bằng chứng |" in prompt
    assert "TOTAL: Planned" in prompt


@pytest.mark.asyncio
async def test_flash_save_note_writes_results_md_in_turn_directory(runner, tmp_path):
    save = {"id": "s1", "name": "save_note", "args": {"key": "results", "content": TABLE}}
    done = {"id": "r1", "name": "report_task_status", "args": {"status": "failed"}}
    runner._invoke_model = AsyncMock(
        side_effect=[
            AIMessage(content="Plan", tool_calls=[save]),
            AIMessage(content="x", tool_calls=[done]),
        ]
    )
    with patch(
        "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
        AsyncMock(return_value=(None, b"image", [])),
    ):
        await runner.run(State(initial_goal=VPN_PROMPT))

    text = (tmp_path / "notes" / "results.md").read_text(encoding="utf-8")
    assert text.count("NOT TESTED |") == 5
    assert "TOTAL: Planned 5" in text


@pytest.mark.asyncio
async def test_update_note_rewrites_one_row(tmp_path):
    executor = McpActionExecutor(_context(tmp_path), actuator=_actuator(), agent_name="flash")
    await executor.execute("save_note", {"key": "results", "content": TABLE}, "a", None)
    result = await executor.execute(
        "update_note",
        {
            "key": "results",
            "target": ROWS[0],
            "replacement": "| Phiên 1 | PASS | 12 server | step 4 |",
        },
        "b",
        None,
    )
    assert result.status == "success"
    text = (tmp_path / "notes" / "results.md").read_text(encoding="utf-8")
    assert "| Phiên 1 | PASS | 12 server | step 4 |" in text
    assert text.count("NOT TESTED |") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["../escape", "a/b", "..", "", "x" * 200])
async def test_unsafe_note_key_writes_nothing(tmp_path, key):
    base = tmp_path / "turn"
    executor = McpActionExecutor(_context(base), actuator=_actuator(), agent_name="flash")
    result = await executor.execute("save_note", {"key": key, "content": "x"}, "a", None)
    assert result.status == "error"
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
