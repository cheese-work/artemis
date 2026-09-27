import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, Mock

from langchain_core.messages import AIMessage
import pytest

from artemis.agents.operator import jev_fast_lane, operator
from artemis.agents.operator.prompts import unwritten_action_streak
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.data_engine.engine import DataEngine
from artemis.graph.graph import execution_check_node
from artemis.graph.state import State
from artemis.memory.transcript import TranscriptLedger
from artemis.services.jev import ChoiceAnswer, JevError


@pytest.fixture
def setup_lane(monkeypatch):
    monkeypatch.setattr(operator.settings, "ARTEMIS_JEV_FAST_LANE", "on")
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.actuator = None
    ctx.data_engine = None
    ctx.transcript_ledger = None
    ctx.package_cache = {"Clock": "com.android.deskclock"}
    node = operator.OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
    node._get_history_and_plan = Mock(return_value=([], "- [/] Open Clock"))
    node._build_prompt = AsyncMock(return_value=[])
    frontier = [{"action": "press_key", "keycode": "KEYCODE_BACK"}]
    node._invoke_llm_loop = AsyncMock(return_value=(frontier, "Frontier reasoning", None, False))
    monkeypatch.setattr(operator, "get_llm", Mock())
    element = {"index": 1, "text": "Alarms", "center": [100, 200], "bounds": [0, 0, 200, 400]}
    monkeypatch.setattr(
        operator,
        "format_minimal_list_with_elements",
        Mock(return_value=("[1] Alarms", [element], [])),
    )
    state = State.initial("Open Clock")
    state.latest_ui_hierarchy = [{"text": "Alarms", "package": "com.android.deskclock"}]
    state.operator_raw_data = {
        "screenshot_b64": "aW1hZ2U=",
        "xml_hierarchy": [],
        "width": 1080,
        "height": 2400,
    }
    client = MagicMock()
    client.ask = AsyncMock(return_value={"next_move": ChoiceAnswer("tap_1", {"tap_1": 0.95}, 0.95)})
    monkeypatch.setattr(operator, "build_client", Mock(return_value=client))
    return node, state, client


@pytest.mark.asyncio
async def test_fast_lane_skips_prompt_and_frontier_and_preserves_shape(setup_lane):
    node, state, client = setup_lane
    result = await node(state)
    node._build_prompt.assert_not_awaited()
    node._invoke_llm_loop.assert_not_awaited()
    assert result["operator_decision_source"] == "jev"
    assert json.loads(result["structured_decisions"])[0]["action"] == "tap"
    assert "Fast lane (Jev 0.95): Tap Alarms" == result["operator_raw_thinking"]
    assert not node._previous_turn_was_silent()
    sent = json.loads(client.ask.call_args.args[0])
    assert sent["foreground_app"] == "com.android.deskclock"
    assert sent["visible_screen_text"] == ["Alarms"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clock_nodes",
    [
        [{"package": "com.android.deskclock", "text": "Alarms"}],
        [{"packageName": "com.android.deskclock", "text": "Alarms"}],
        [
            {"package": "com.android.launcher3"},
            {
                "children": [
                    {"package": "com.android.deskclock", "text": "Clock"},
                    {"packageName": "com.android.deskclock", "text": "Alarms"},
                ]
            },
        ],
    ],
)
async def test_fast_lane_foreground_ignores_system_overlay(setup_lane, clock_nodes):
    node, state, client = setup_lane
    state.latest_ui_hierarchy = [
        {"package": "com.android.systemui", "text": "12:00"},
        *clock_nodes,
    ]
    result = await node(state)
    assert result["operator_decision_source"] == "jev"
    assert json.loads(client.ask.call_args.args[0])["foreground_app"] == "com.android.deskclock"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "escalate",
        "unknown",
        "low",
        "http",
        "timeout",
        "cardinality",
        "validation",
        "unsupported",
        "no_plan",
        "unconfigured",
    ],
)
async def test_fallbacks_are_frontier_same_turn(setup_lane, monkeypatch, failure):
    node, state, client = setup_lane
    if failure in ("missing", "escalate", "unknown", "low"):
        choice = {"escalate": "escalate", "unknown": "tap_999"}.get(failure, "tap_1")
        client.ask.return_value = (
            {}
            if failure == "missing"
            else {"next_move": ChoiceAnswer(choice, {}, 0.5 if failure == "low" else 0.99)}
        )
    elif failure == "http":
        client.ask.side_effect = JevError("HTTP 503")
    elif failure == "timeout":
        monkeypatch.setattr(jev_fast_lane, "DEFAULT_TIMEOUT_SECONDS", 0.001)

        async def slow(*args):
            await asyncio.sleep(60)

        client.ask.side_effect = slow
    elif failure == "cardinality":
        monkeypatch.setattr(
            operator, "build_moves", lambda *args: {str(number): "target" for number in range(256)}
        )
    elif failure == "validation":
        node._translate_and_validate_tool = Mock(return_value=([], "invalid"))
    elif failure == "unsupported":
        node._available_device_actions = Mock(return_value=frozenset())
    elif failure == "no_plan":
        node._get_history_and_plan.return_value = ([], "- [x] Open Clock")
    elif failure == "unconfigured":
        monkeypatch.setattr(operator, "build_client", Mock(return_value=None))
    result = await node(state)
    assert result["operator_decision_source"] == "frontier"
    assert json.loads(result["structured_decisions"])[0]["keycode"] == "KEYCODE_BACK"
    node._invoke_llm_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_off_has_no_request(setup_lane, monkeypatch):
    node, state, client = setup_lane
    monkeypatch.setattr(operator.settings, "ARTEMIS_JEV_FAST_LANE", "off")
    result = await node(state)
    assert result["operator_decision_source"] == "frontier"
    client.ask.assert_not_awaited()
    operator.build_client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("injected_instruction", "Stop"),
        ("user_stop_requested", True),
        ("open_incident", {"reason": "failed"}),
        ("operator_feedback", [{"reason": "retry"}]),
    ],
)
async def test_frontier_handles_instructions_stop_and_recovery(setup_lane, field, value):
    node, state, client = setup_lane
    setattr(state, field, value)
    result = await node(state)
    assert result["operator_decision_source"] == "frontier"
    client.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_shadow_runs_in_parallel_and_never_acts(setup_lane, monkeypatch):
    node, state, client = setup_lane
    monkeypatch.setattr(operator.settings, "ARTEMIS_JEV_FAST_LANE", "shadow")
    answered = asyncio.Event()
    original_answer = client.ask.return_value

    async def ask(*args):
        answered.set()
        return original_answer

    async def frontier(**kwargs):
        await answered.wait()
        return ([{"action": "press_key", "keycode": "KEYCODE_BACK"}], "Frontier", None, False)

    client.ask.side_effect = ask
    node._invoke_llm_loop.side_effect = frontier
    node._translate_and_validate_tool = Mock(side_effect=AssertionError("shadow acted"))
    node.ctx.data_engine = MagicMock()
    result = await node(state)
    assert result["operator_decision_source"] == "frontier"
    payload = next(
        call.kwargs["payload"]
        for call in node.ctx.data_engine.record_trace.call_args_list
        if call.kwargs.get("name") == "fast_lane"
    )
    assert payload["choice"] == "tap_1" and payload["taken"] is False
    assert set(payload) == {
        "mode",
        "choice",
        "confidence",
        "probabilities_top3",
        "gate_result",
        "gate_reason",
        "latency_s",
        "taken",
        "move_count",
        "launchable_apps",
        "model",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("frontier_error", [False, True])
async def test_slow_shadow_is_cancelled_without_waiting_for_timeout(
    setup_lane, monkeypatch, frontier_error
):
    node, state, client = setup_lane
    monkeypatch.setattr(operator.settings, "ARTEMIS_JEV_FAST_LANE", "shadow")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async def frontier(**kwargs):
        await started.wait()
        if frontier_error:
            raise RuntimeError("frontier failed")
        return ([], "Frontier", None, False)

    client.ask.side_effect = slow
    node._invoke_llm_loop.side_effect = frontier
    if frontier_error:
        with pytest.raises(RuntimeError, match="frontier failed"):
            await asyncio.wait_for(node(state), 0.2)
    else:
        result = await asyncio.wait_for(node(state), 0.2)
        assert result["operator_decision_source"] == "frontier"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_cancelled_frontier_cancels_shadow_and_propagates(setup_lane, monkeypatch):
    node, state, client = setup_lane
    monkeypatch.setattr(operator.settings, "ARTEMIS_JEV_FAST_LANE", "shadow")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    async def frontier(**kwargs):
        await asyncio.sleep(60)

    client.ask.side_effect = slow
    node._invoke_llm_loop.side_effect = frontier
    task = asyncio.create_task(node(state))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_transcript_commits_each_fast_turn_with_step_and_result(setup_lane):
    node, state, client = setup_lane
    node._transcript_cfg = MemoryTranscriptConfig(enabled=True)
    ledger = TranscriptLedger()
    node.ctx.transcript_ledger = ledger
    ledger.stage_turn([AIMessage(content="Frontier before fast lane")])
    state.structured_decisions = "[]"
    state.current_step_id = "frontier-step"
    state.last_execution_result = {"status": "success"}
    first = await node(state)
    assert ledger.turn_count == 1
    assert ledger._turns[0]["step_key"] == "frontier-step"
    state.current_step_id = "jev-step"
    state.structured_decisions = first["structured_decisions"]
    await node(state)
    assert ledger.turn_count == 2
    assert ledger._turns[1]["step_key"] == "jev-step"
    assert any("Fast lane (Jev" in str(message.content) for message in ledger.active_messages)
    assert not ledger.last_turn_silent and ledger.has_staged_turn


def test_ledger_excludes_jev_but_old_frontier_rows_count():
    action = {"action_taken": [{"action": "tap"}]}
    fast = {**action, "extra_metadata": {"decision_source": "jev"}}
    assert unwritten_action_streak([action, fast, fast, action, fast]) == 2


@pytest.mark.asyncio
async def test_db_source_streak_and_turn_recording(setup_lane, tmp_path, monkeypatch):
    node, state, client = setup_lane
    ctx = node.ctx
    ctx.execution_setup = ExecutionSetup(traces_path=str(tmp_path), disable_checker=True)
    ctx.device = None
    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda self, **kwargs: False)
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session("Clock fixture")
    engine._run_in_background = lambda callback, *args, **kwargs: callback(*args, **kwargs)
    for _ in range(3):
        engine.allocate_step_id()
        engine.record_step(
            action_taken=[{"action": "tap"}], extra_metadata={"decision_source": "jev"}
        )
    plan_dir = engine.base_dir / "notes"
    plan_dir.mkdir(exist_ok=True)
    (plan_dir / "task_plan.md").write_text("- [/] Open Clock", encoding="utf-8")
    node._get_history_and_plan = operator.OperatorNode._get_history_and_plan.__get__(node)
    ctx.planner_task = None
    ctx.checkpoint_tasks = {}
    ctx.pending_checkpoints = []
    ctx.pending_validated_plan = None
    engine.allocate_step_id()
    result = await node(state)
    assert result["operator_decision_source"] == "frontier"
    traces = engine.storage.get_step_traces(engine.current_step_id)
    assert any(
        trace.name == "fast_lane" and trace.payload["gate_reason"] == "streak" for trace in traces
    )
    state = State(**{**state.model_dump(), **result})
    await execution_check_node(state, ctx)
    steps = engine.get_agent_friendly_steps()
    assert steps[-1]["extra_metadata"]["decision_source"] == "frontier"
    engine.allocate_step_id()
    result = await node(state)
    assert result["operator_decision_source"] == "jev"
    state = State(**{**state.model_dump(), **result})
    await execution_check_node(state, ctx)
    assert engine.get_agent_friendly_steps()[-1]["extra_metadata"]["decision_source"] == "jev"
    await engine.shutdown()


@pytest.mark.asyncio
async def test_fourth_jev_turn_falls_back_while_third_write_is_pending(
    setup_lane, tmp_path, monkeypatch
):
    node, state, client = setup_lane
    ctx = node.ctx
    ctx.execution_setup = ExecutionSetup(traces_path=str(tmp_path), disable_checker=True)
    ctx.device = None
    ctx.planner_task = None
    ctx.checkpoint_tasks = {}
    ctx.pending_checkpoints = []
    ctx.pending_validated_plan = None
    monkeypatch.setattr(DataEngine, "_connect_ipc_locked", lambda self, **kwargs: False)
    engine = DataEngine(ctx)
    ctx.data_engine = engine
    engine.start_session("Delayed Jev persistence fixture")
    release_writer = threading.Event()
    writer_started = threading.Event()
    create_step = engine.storage.create_step

    def delayed_create_step(step):
        if step.step_number == 3:
            writer_started.set()
            if not release_writer.wait(10):
                raise TimeoutError("Jev persistence fixture was not released")
        create_step(step)

    try:
        for _ in range(2):
            engine.allocate_step_id()
            engine.record_step(
                action_taken=[{"action": "tap"}], extra_metadata={"decision_source": "jev"}
            )
        await asyncio.gather(*engine._pending_tasks)
        assert len(engine.get_agent_friendly_steps()) == 2
        plan_dir = engine.base_dir / "notes"
        plan_dir.mkdir(exist_ok=True)
        (plan_dir / "task_plan.md").write_text("- [/] Open Clock", encoding="utf-8")
        node._get_history_and_plan = operator.OperatorNode._get_history_and_plan.__get__(node)
        monkeypatch.setattr(engine.storage, "create_step", delayed_create_step)

        engine.allocate_step_id()
        third = await node(state)
        assert third["operator_decision_source"] == "jev"
        state = State(**{**state.model_dump(), **third})
        recorded = await execution_check_node(state, ctx)
        state = State(**{**state.model_dump(), **recorded})
        assert await asyncio.to_thread(writer_started.wait, 2)
        assert engine.current_step_number == 3
        assert len(engine.get_agent_friendly_steps()) == 2

        fourth_step_id = engine.allocate_step_id()
        fourth = await asyncio.wait_for(node(state), 1)
        assert fourth["operator_decision_source"] == "frontier"
        assert not release_writer.is_set()
        node._invoke_llm_loop.assert_awaited_once()
        client.ask.assert_awaited_once()

        release_writer.set()
        await asyncio.gather(*engine._pending_tasks)
        assert len(engine.get_agent_friendly_steps()) == 3
        traces = engine.storage.get_step_traces(fourth_step_id)
        assert any(
            trace.name == "fast_lane" and trace.payload["gate_reason"] == "history_pending"
            for trace in traces
        )

        assert (await node(state))["operator_decision_source"] == "frontier"

        state = State(**{**state.model_dump(), **fourth})
        await execution_check_node(state, ctx)
        await asyncio.gather(*engine._pending_tasks)
        engine.allocate_step_id()
        assert (await node(state))["operator_decision_source"] == "jev"
    finally:
        release_writer.set()
        await engine.shutdown()
