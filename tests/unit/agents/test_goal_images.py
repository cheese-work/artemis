# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pictures attached to a goal reach the model next to the goal text (CHE-1242).

The console hands the worker the stored files through ``ARTEMIS_GOAL_IMAGES``;
each agent that reads the goal then also sends the pictures, and a picture that
cannot be read stops the run instead of being dropped.
"""

import base64
import json
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.messages import HumanMessage, SystemMessage
import pytest

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.operator.prompts import ObservationPromptComponent, PromptBuilder
from artemis.agents.planner.planner import PlannerNode
from artemis.context import ArtemisContext
from artemis.utils import goal_images

PNG = b"\x89PNG\r\n\x1a\nfake-png"
JPG = b"\xff\xd8\xff\xe0fake-jpg"


@pytest.fixture
def attached(tmp_path, monkeypatch):
    first, second = tmp_path / "0.png", tmp_path / "1.jpg"
    first.write_bytes(PNG)
    second.write_bytes(JPG)
    monkeypatch.setenv(goal_images.ENV_VAR, json.dumps([str(first), str(second)]))
    return first, second


def _image_urls(blocks) -> list[str]:
    return [b["image_url"]["url"] for b in blocks if b.get("type") == "image_url"]


def test_no_variable_means_no_blocks_and_no_message(monkeypatch):
    monkeypatch.delenv(goal_images.ENV_VAR, raising=False)

    assert goal_images.goal_image_blocks() == []
    assert goal_images.goal_image_messages() == []


def test_each_file_becomes_an_image_block_with_its_real_media_type(attached):
    blocks = goal_images.goal_image_blocks()

    assert blocks[0]["type"] == "text" and "attached" in blocks[0]["text"]
    assert _image_urls(blocks) == [
        "data:image/png;base64," + base64.b64encode(PNG).decode(),
        "data:image/jpeg;base64," + base64.b64encode(JPG).decode(),
    ]


def test_the_message_form_is_one_human_message(attached):
    [message] = goal_images.goal_image_messages()

    assert isinstance(message, HumanMessage)
    assert len(_image_urls(message.content)) == 2


@pytest.mark.parametrize("value", ["not json", '{"a": 1}', '["relative/0.png"]', "[1]"])
def test_a_bad_variable_stops_the_run(monkeypatch, value):
    monkeypatch.setenv(goal_images.ENV_VAR, value)

    with pytest.raises(goal_images.GoalImageError):
        goal_images.goal_image_blocks()


def test_a_missing_or_unsupported_file_stops_the_run_instead_of_dropping_it(tmp_path, monkeypatch):
    gone = tmp_path / "0.png"
    odd = tmp_path / "1.gif"
    odd.write_bytes(b"GIF89a")
    for path in (gone, odd):
        monkeypatch.setenv(goal_images.ENV_VAR, json.dumps([str(path)]))
        with pytest.raises(goal_images.GoalImageError):
            goal_images.goal_image_blocks()


# -- every agent that reads the goal sends the pictures ----------------------------


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    cfg = Mock(model="gemini-2.5-flash", provider="google", temperature=0.1)
    ctx.llm_config.get_agent.return_value = cfg
    ctx.device = Mock(device_width=1080, device_height=2400)
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_provider_credentials")
async def test_flash_sends_the_pictures_right_after_the_system_prompt(mock_context, attached):
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            new=AsyncMock(return_value=("p", b"jpeg", "xml")),
        ),
    ):
        runner = FlashRunner(mock_context, goal="describe the picture")
        ledger, _, _ = await runner._prepare_conversation(Mock(), [])

    system, pictures = ledger.render([])
    assert isinstance(system, SystemMessage)
    assert len(_image_urls(pictures.content)) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_provider_credentials")
async def test_flash_without_pictures_is_unchanged(mock_context, monkeypatch):
    monkeypatch.delenv(goal_images.ENV_VAR, raising=False)
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            new=AsyncMock(return_value=("p", b"jpeg", "xml")),
        ),
    ):
        runner = FlashRunner(mock_context, goal="g")
        ledger, _, _ = await runner._prepare_conversation(Mock(), [])

    assert [type(m) for m in ledger.render([])] == [SystemMessage]


@pytest.mark.asyncio
async def test_the_operator_observation_carries_the_pictures(attached):
    builder = PromptBuilder()

    await ObservationPromptComponent()(
        builder, Mock(), Mock(), latest_screenshot_b64="c2hvdA==", minimal_list="[]"
    )

    urls = _image_urls([p for p in builder.human_parts if isinstance(p, dict)])
    assert urls[:2] == _image_urls(goal_images.goal_image_blocks())
    assert urls[-1].endswith("c2hvdA==")


@pytest.mark.asyncio
async def test_the_planner_sends_the_pictures_with_the_first_screenshot(mock_context, attached):
    state = Mock(initial_goal="g", latest_screenshot=None)
    node = PlannerNode(mock_context)
    llm = Mock()
    response = Mock(content="- [ ] one", tool_calls=[])

    async def astream(*_a, **_k):
        yield response

    llm.astream.side_effect = astream
    llm.bind_tools = Mock(return_value=llm)
    with (
        patch("artemis.agents.planner.planner.get_llm", return_value=llm),
        patch("artemis.agents.planner.planner.UnifiedMobileController") as controller,
    ):
        controller.return_value.take_screenshot = AsyncMock(return_value="c2hvdA==")
        await node(state)

    human = llm.astream.call_args.args[0][1]
    assert _image_urls(human.content)[:2] == _image_urls(goal_images.goal_image_blocks())
    assert _image_urls(human.content)[-1].endswith("c2hvdA==")
