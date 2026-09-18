"""Unit tests for Anthropic prompt-cache breakpoints (CHE-658).

Covers the two placements (system prompt, transcript anchor), the four-
breakpoint ceiling, Anthropic-only application through the
:class:`~artemis.services.llm.RobustChatModelWrapper` chokepoint, idempotency,
and the no-caller-mutation contract. The horizon test replays the real
:class:`~artemis.memory.transcript.TranscriptLedger` and asserts the marked
index is strictly older than the one message that differs between consecutive
renders — the same technique as
``tests/unit/memory/test_transcript_ledger.py::test_rendering_past_the_edge_is_byte_identical_and_prefix_stable``.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from artemis.llm.anthropic_cache import (
    CACHE_CONTROL,
    ENV_FLAG,
    MAX_BREAKPOINTS,
    apply_cache_breakpoints,
    mutation_horizon_depths,
    stable_prefix_index,
)
from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import PLAN_RECITATION_MARKER, PRO_UI_LIST_MARKER, TranscriptLedger
from artemis.services.llm import RobustChatModelWrapper

HORIZON = 3


def _breakpoint_indexes(messages: list) -> list[int]:
    marked = []
    for idx, message in enumerate(messages):
        content = message.content
        if isinstance(content, list) and any(
            isinstance(b, dict) and "cache_control" in b for b in content
        ):
            marked.append(idx)
    return marked


def _fingerprint(message) -> str:
    return json.dumps(message.content, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Ledger replay helpers (mirrors tests/unit/memory/test_transcript_ledger.py)
# ---------------------------------------------------------------------------


def _ready_service(count: int) -> StepMemoryService:
    service = StepMemoryService(ctx=None)
    for i in range(count):
        service._summaries[f"step-{i}"] = f"summary of step {i}"
    return service


def _observation(i: int) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": f"# CURRENT OBSERVATION [T+00:0{i % 10}]"},
            {"type": "text", "text": f"{PLAN_RECITATION_MARKER}\n- [/] milestone {i}"},
            {"type": "text", "text": "--- Current Screenshot ---"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,IMG_{i}"}},
            {"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n[1] button {i}"},
        ]
    )


def _turn(i: int) -> list:
    tool_call = {"name": "click", "args": {"target": 1}, "id": f"tc{i}", "type": "tool_call"}
    return [
        _observation(i),
        AIMessage(content=f"thinking {i}", tool_calls=[tool_call]),
        ToolMessage(tool_call_id=tool_call["id"], content="Action Recorded"),
    ]


def _play_turn(ledger: TranscriptLedger, i: int) -> list:
    """The operator's real ordering: commit the previous turn, render, stage."""
    ledger.commit_staged(
        step_key=f"step-{i - 1}" if i > 1 else None,
        validator_result={"status": "dispatched"} if i > 1 else None,
    )
    turn = _turn(i)
    rendered = ledger.render([turn[0]])
    ledger.stage_turn(turn)
    return rendered


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_system_prompt_carries_a_breakpoint():
    """The leading system run caches the tools + system prefix."""
    messages = [SystemMessage(content="STATIC SYSTEM"), HumanMessage(content="go")]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert _breakpoint_indexes(marked) == [0]
    assert marked[0].content == [
        {"type": "text", "text": "STATIC SYSTEM", "cache_control": CACHE_CONTROL}
    ]


def test_last_message_of_the_leading_system_run_is_the_anchor():
    """Two system messages: only the newest of the run carries the breakpoint."""
    messages = [
        SystemMessage(content="ROLE"),
        SystemMessage(content="TOOLS"),
        HumanMessage(content="go"),
    ]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert _breakpoint_indexes(marked) == [1]


def test_transcript_breakpoint_lands_behind_the_mutation_horizon():
    """The anchor is strictly older than the message that still mutates.

    Exactly one message differs between consecutive renders — the observation
    reaching the screenshot edge. The breakpoint must sit before it, or every
    turn would pay a fresh cache write instead of a cache read.
    """
    ledger = TranscriptLedger(step_memory=_ready_service(24), image_scrub_depth=HORIZON)
    ledger.set_static_prefix([SystemMessage(content="STATIC")])

    previous: list[str] = []
    checked = 0
    for i in range(1, 17):
        rendered = _play_turn(ledger, i)
        current = [_fingerprint(m) for m in rendered[:-1]]
        changed = [idx for idx, fp in enumerate(previous) if current[idx] != fp]
        previous = current

        marked = apply_cache_breakpoints(rendered, horizon_depths=HORIZON)
        indexes = _breakpoint_indexes(marked)
        if len(indexes) < 2:
            continue
        transcript_index = indexes[-1]
        assert transcript_index < len(rendered) - 1, "never the live tail"
        assert changed, f"turn {i}: expected a mutating message to compare against"
        assert transcript_index < min(changed), (i, transcript_index, changed)
        checked += 1

    assert checked >= 5, "the transcript anchor never engaged; the replay is too short"


def test_short_conversation_gets_no_transcript_breakpoint():
    """Fewer human messages than the horizon proves nothing: system only."""
    messages = [SystemMessage(content="STATIC")]
    messages += [HumanMessage(content=f"turn {i}") for i in range(2 * HORIZON)]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert _breakpoint_indexes(marked) == [0]
    assert stable_prefix_index(messages, HORIZON) is None


def test_no_system_prompt_still_works():
    """A messages-only request gets the transcript anchor and does not crash."""
    messages = [HumanMessage(content=f"turn {i}") for i in range(4 * HORIZON)]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert _breakpoint_indexes(marked) == [len(messages) - (2 * HORIZON + 1)]


def test_breakpoint_count_never_exceeds_the_api_ceiling():
    """Anthropic rejects more than four breakpoints per request."""
    ledger = TranscriptLedger(step_memory=_ready_service(24), image_scrub_depth=HORIZON)
    ledger.set_static_prefix([SystemMessage(content="STATIC")])
    for i in range(1, 17):
        rendered = _play_turn(ledger, i)
        marked = apply_cache_breakpoints(rendered, horizon_depths=HORIZON)
        assert len(_breakpoint_indexes(marked)) <= MAX_BREAKPOINTS


def test_mutation_horizon_matches_the_configured_scrub_depths():
    """image_scrub_depth_relaxed (6) + pending_grace_steps (3) = 9."""
    assert mutation_horizon_depths() == 9


# ---------------------------------------------------------------------------
# Safety contracts
# ---------------------------------------------------------------------------


def test_applying_twice_does_not_double_mark():
    messages = [SystemMessage(content="STATIC")]
    messages += [HumanMessage(content=f"turn {i}") for i in range(4 * HORIZON)]

    once = apply_cache_breakpoints(messages, horizon_depths=HORIZON)
    twice = apply_cache_breakpoints(once, horizon_depths=HORIZON)

    assert twice is once
    assert _breakpoint_indexes(twice) == _breakpoint_indexes(once)


def test_callers_messages_are_not_mutated():
    system = SystemMessage(content="STATIC")
    observation = HumanMessage(content=[{"type": "text", "text": "obs"}])
    messages = [system, observation]
    messages += [HumanMessage(content=f"turn {i}") for i in range(4 * HORIZON)]
    before = [_fingerprint(m) for m in messages]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert marked is not messages
    assert [_fingerprint(m) for m in messages] == before
    assert system.content == "STATIC"
    assert observation.content == [{"type": "text", "text": "obs"}]


def test_opt_out_env_var_returns_the_caller_list(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "0")
    messages = [SystemMessage(content="STATIC"), HumanMessage(content="go")]

    assert apply_cache_breakpoints(messages, horizon_depths=HORIZON) is messages


# ---------------------------------------------------------------------------
# Provider gating at the wrapper chokepoint
# ---------------------------------------------------------------------------


class _RecordingModel:
    """Minimal chat model double that records the messages it was handed."""

    def __init__(self):
        self.seen: list = []

    async def ainvoke(self, messages, *args, **kwargs):
        self.seen = messages
        return AIMessage(content="ok")


def _wrapped(provider: ModelProvider) -> tuple[RobustChatModelWrapper, _RecordingModel]:
    base = _RecordingModel()
    endpoint = ModelEndpoint(provider=provider, model_name="test-model", api_key="test-key")
    return RobustChatModelWrapper(base, None, endpoint=endpoint), base


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [ModelProvider.OPENAI, ModelProvider.GOOGLE])
async def test_non_anthropic_providers_get_an_unchanged_request(provider):
    """OpenAI and Google already cache prefixes server-side: no shape change."""
    wrapper, base = _wrapped(provider)
    messages = [SystemMessage(content="STATIC")]
    messages += [HumanMessage(content=f"turn {i}") for i in range(40)]
    before = [_fingerprint(m) for m in messages]

    await wrapper.ainvoke(messages)

    assert base.seen is messages
    assert [_fingerprint(m) for m in base.seen] == before
    assert _breakpoint_indexes(base.seen) == []


@pytest.mark.asyncio
async def test_anthropic_requests_are_marked_at_the_chokepoint():
    wrapper, base = _wrapped(ModelProvider.ANTHROPIC)
    messages = [SystemMessage(content="STATIC")]
    messages += [HumanMessage(content=f"turn {i}") for i in range(40)]

    await wrapper.ainvoke(messages)

    assert base.seen is not messages
    assert len(_breakpoint_indexes(base.seen)) == 2
    assert _breakpoint_indexes(messages) == []
