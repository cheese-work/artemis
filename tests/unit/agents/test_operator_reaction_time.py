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

"""Tests for CHE-645 Operator tool-loop iteration classification.

Each test drives ``OperatorNode.__call__`` (or ``_invoke_llm_loop`` via it)
through a real LLM mock and asserts on the ``phase:operator_iteration``
spans recorded through ``ctx.data_engine.record_trace`` — the exact
observation surface ``reaction_time.py``'s aggregation reads. Classification
must be precise: a plan-ledger bounce must never show up as generic
``validation_errors``, etc.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.core.tool_failure import ToolFailure
from artemis.data_engine.reaction_time import OperatorIterationOutcome, ReactionPhase

LEGACY_TRANSCRIPT = MemoryTranscriptConfig(enabled=False)


def _base_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = MagicMock()
    return ctx


def _base_state():
    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "Test goal"
    state.open_incident = None
    return state


def _click_turn(n=1, call_prefix="call_click"):
    turn = MagicMock()
    turn.tool_calls = [
        {
            "name": "click",
            "args": {"target": [50, 50], "target_description": f"button {i}"},
            "id": f"{call_prefix}_{i}",
        }
        for i in range(n)
    ]
    return turn


def _empty_turn():
    turn = MagicMock()
    turn.tool_calls = []
    return turn


def _iteration_spans(ctx):
    """Every ``phase:operator_iteration`` span recorded on the mock data
    engine, as ``(iteration, outcome)`` pairs, in call order."""
    spans = []
    for call in ctx.data_engine.record_trace.call_args_list:
        kwargs = call.kwargs
        if (
            kwargs.get("type") == "span"
            and kwargs.get("name") == ReactionPhase.OPERATOR_ITERATION.value
        ):
            payload = kwargs["payload"]
            spans.append((payload["iteration"], payload["outcome"]))
    return spans


def _passthrough_tool(name: str, result):
    tool = MagicMock()
    tool.name = name
    tool.args = {}

    async def _coro(**kwargs):
        return result

    tool.coroutine = _coro
    tool.func = None
    return tool


@pytest.mark.asyncio
async def test_executed_outcome_recorded():
    ctx = _base_ctx()
    state = _base_state()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=_click_turn(1))
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [(0, OperatorIterationOutcome.EXECUTED.value)]


@pytest.mark.asyncio
async def test_no_tool_call_outcome_recorded():
    ctx = _base_ctx()
    state = _base_state()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=_empty_turn())
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [(0, OperatorIterationOutcome.NO_TOOL_CALL.value)]


@pytest.mark.asyncio
async def test_helper_tool_failure_outcome_recorded_then_executed():
    ctx = _base_ctx()
    state = _base_state()

    turn_1 = MagicMock()
    turn_1.tool_calls = [
        {"name": "read_note", "args": {"key": "progress"}, "id": "call_read"},
        {
            "name": "click",
            "args": {"target": [50, 50], "target_description": "button"},
            "id": "call_click",
        },
    ]
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[turn_1, _click_turn(1, "call_click_final")])
    mock_llm.bind_tools.return_value = mock_llm

    read_note_tool = _passthrough_tool("read_note", ToolFailure("Error: note 'progress' not found"))

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(ctx, tools=[read_note_tool], transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [
        (0, OperatorIterationOutcome.HELPER_TOOL_FAILURE.value),
        (1, OperatorIterationOutcome.EXECUTED.value),
    ]


@pytest.mark.asyncio
async def test_deferring_tool_mix_outcome_recorded_then_executed():
    ctx = _base_ctx()
    state = _base_state()

    turn_1 = MagicMock()
    turn_1.tool_calls = [
        {"name": "list_notes", "args": {}, "id": "call_list"},
        {
            "name": "click",
            "args": {"target": [50, 50], "target_description": "button"},
            "id": "call_click",
        },
    ]
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[turn_1, _click_turn(1, "call_click_final")])
    mock_llm.bind_tools.return_value = mock_llm

    list_notes_tool = _passthrough_tool("list_notes", "note_a\nnote_b")

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(ctx, tools=[list_notes_tool], transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [
        (0, OperatorIterationOutcome.DEFERRING_TOOL_MIX.value),
        (1, OperatorIterationOutcome.EXECUTED.value),
    ]


@pytest.mark.asyncio
async def test_burst_limit_outcome_recorded_then_executed():
    ctx = _base_ctx()
    state = _base_state()

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        side_effect=[_click_turn(6, "call_burst"), _click_turn(1, "call_click_final")]
    )
    mock_llm.bind_tools.return_value = mock_llm

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch.object(OperatorNode, "_max_burst_actions", return_value=4),
    ):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [
        (0, OperatorIterationOutcome.BURST_LIMIT.value),
        (1, OperatorIterationOutcome.EXECUTED.value),
    ]


@pytest.mark.asyncio
async def test_generic_validation_errors_outcome_recorded_then_executed():
    """A per-action translation failure with no more-specific label
    (no plan-ledger bounce, no burst overflow) reports the generic
    ``validation_errors`` outcome — never silently folded into
    ``executed`` or misreported as a more specific label."""
    ctx = _base_ctx()
    state = _base_state()

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        side_effect=[_click_turn(1, "call_bad"), _click_turn(1, "call_click_final")]
    )
    mock_llm.bind_tools.return_value = mock_llm

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch.object(
            OperatorNode,
            "_translate_and_validate_tool",
            side_effect=[([], "Error: invalid target"), ([{"action": "tap"}], None)],
        ),
    ):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    assert _iteration_spans(ctx) == [
        (0, OperatorIterationOutcome.VALIDATION_ERRORS.value),
        (1, OperatorIterationOutcome.EXECUTED.value),
    ]


@pytest.mark.asyncio
async def test_plan_ledger_gate_outcome_never_reports_as_generic_validation_errors(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True)
    # A broken ledger (no in-progress leaf under the active milestone)
    # triggers the plan-ledger gate on a single-action turn.
    (notes / "task_plan.md").write_text(
        "- [x] Open Google Maps and search for SFO\n"
        "- [/] Read the commute duration and record it into note `commute_eta_info`\n"
        "- [ ] Draft the ETA message\n",
        encoding="utf-8",
    )

    ctx = _base_ctx()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.current_session_id = "s"
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    state = _base_state()

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        side_effect=[_click_turn(1, "call_bounced"), _click_turn(1, "call_click_final")]
    )
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        await node(state)

    spans = _iteration_spans(ctx)
    assert spans == [
        (0, OperatorIterationOutcome.PLAN_LEDGER_GATE.value),
        (1, OperatorIterationOutcome.EXECUTED.value),
    ]
    # Never the generic fallback — the more specific label must win.
    assert OperatorIterationOutcome.VALIDATION_ERRORS.value not in [o for _, o in spans]


@pytest.mark.asyncio
async def test_tool_limit_exceeded_outcome_on_final_iteration():
    ctx = _base_ctx()
    state = _base_state()

    # Every turn calls only a non-terminal helper tool (no screen action),
    # so the loop never breaks and must exhaust every iteration.
    stall_turn = MagicMock()
    stall_turn.tool_calls = [{"name": "list_notes", "args": {}, "id": "call_list"}]
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=stall_turn)
    mock_llm.bind_tools.return_value = mock_llm

    list_notes_tool = _passthrough_tool("list_notes", "note_a\nnote_b")

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch("artemis.agents.operator.operator.OPERATOR_MAX_TOOL_ITERATIONS", 2),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(ctx, tools=[list_notes_tool], transcript_config=LEGACY_TRANSCRIPT)
        update = await node(state)

    assert _iteration_spans(ctx) == [
        (0, OperatorIterationOutcome.CONTINUED.value),
        (1, OperatorIterationOutcome.TOOL_LIMIT_EXCEEDED.value),
    ]
    assert update.get("operator_tool_limit_exceeded") is True


@pytest.mark.asyncio
async def test_none_data_engine_never_crashes_classification():
    """``ctx.data_engine`` may legitimately be ``None`` (e.g. tracing
    disabled); classification must still run to completion with no span
    recorded and no exception."""
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = None
    state = _base_state()

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=_click_turn(1))
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
        update = await node(state)

    assert update["structured_decisions"] is not None
