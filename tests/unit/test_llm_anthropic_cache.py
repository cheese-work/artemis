"""Unit tests for Anthropic prompt-cache breakpoints (CHE-658).

Covers the three placements (system prompt, conservative transcript anchor,
exact transcript anchor), the four-breakpoint ceiling, Anthropic-only
application through the :class:`~artemis.services.llm.RobustChatModelWrapper`
chokepoint, idempotency, and the no-caller-mutation contract.

The horizon tests replay the real
:class:`~artemis.memory.transcript.TranscriptLedger` and diff consecutive
renders — the same technique as
``tests/unit/memory/test_transcript_ledger.py::test_rendering_past_the_edge_is_byte_identical_and_prefix_stable``.
They compare *forward*: an anchor placed at turn i has to be older than every
message that changes at turn i+1 and after, because that is the prefix the
provider has to find byte-identical on the next request.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from artemis.agents.flash.context_compressor import ScrubEdgeCompressor
from artemis.llm.anthropic_cache import (
    CACHE_CONTROL,
    ENV_FLAG,
    MAX_BREAKPOINTS,
    apply_cache_breakpoints,
    frozen_prefix_index,
    horizon_prefix_index,
    mutation_horizon_depths,
    stable_prefix_index,
    transcript_anchors,
)
from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.memory.scrub_shape import has_pending_scrub_edits, is_screenshot_observation
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import PLAN_RECITATION_MARKER, PRO_UI_LIST_MARKER, TranscriptLedger
from artemis.services.llm import RobustChatModelWrapper

HORIZON = 3

#: The production horizon and the screenshot depth it is derived from: the
#: relaxed depth (6) plus the pending grace window (3). The run that motivated
#: this module ran 13 operator turns at exactly these settings and got no
#: transcript breakpoint at all.
REAL_HORIZON = 9
REAL_SCRUB_DEPTH = 6


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


def _play_turn(ledger: TranscriptLedger, i: int, *, with_result: bool = True) -> list:
    """The operator's real ordering: commit the previous turn, render, stage.

    ``with_result=False`` is the planning turn shape: ``Operator`` passes a
    validator result only for a turn that reached a terminal action
    (``artemis/agents/operator/operator.py:404``), so such a turn adds one
    HumanMessage to the render instead of two.
    """
    ledger.commit_staged(
        step_key=f"step-{i - 1}" if i > 1 else None,
        validator_result={"status": "dispatched"} if i > 1 and with_result else None,
    )
    turn = _turn(i)
    rendered = ledger.render([turn[0]])
    ledger.stage_turn(turn)
    return rendered


def _fresh_ledger(scrub_depth: int = HORIZON) -> TranscriptLedger:
    ledger = TranscriptLedger(step_memory=_ready_service(40), image_scrub_depth=scrub_depth)
    ledger.set_static_prefix([SystemMessage(content="STATIC")])
    return ledger


def _replay(turns: int, scrub_depth: int = HORIZON, *, with_result: bool = True) -> list:
    """The render an operator would send on its ``turns``-th step."""
    ledger = _fresh_ledger(scrub_depth)
    rendered: list = []
    for i in range(1, turns + 1):
        rendered = _play_turn(ledger, i, with_result=with_result)
    return rendered


def _old_stable_prefix_index(messages: list, horizon_depths: int) -> int | None:
    """The rule this module replaced: the ``2 * horizon + 1``-th newest human.

    Kept verbatim so the regression test can state the improvement in the same
    terms as the bug report instead of asserting a remembered number.
    """
    needed = 2 * max(1, horizon_depths) + 1
    humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    return humans[-needed] if len(humans) >= needed else None


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


@pytest.mark.parametrize(
    ("horizon", "scrub_depth", "with_result"),
    [
        (HORIZON, HORIZON, True),
        (REAL_HORIZON, REAL_SCRUB_DEPTH, True),
        (REAL_HORIZON, REAL_SCRUB_DEPTH, False),
    ],
)
def test_transcript_breakpoints_stay_behind_every_later_mutation(horizon, scrub_depth, with_result):
    """Every anchor ever placed is older than everything that changes after it.

    Exactly one message differs between consecutive renders — the observation
    reaching the screenshot edge. An anchor placed on turn i is the promise
    that the prefix up to it is byte-identical on turn i+1 and on every turn
    after that, so it is checked against all later diffs, not just the one that
    happened to precede it.
    """
    ledger = _fresh_ledger(scrub_depth)

    previous: list[str] = []
    placed: list[tuple[int, int]] = []
    checked = 0
    for i in range(1, 21):
        rendered = _play_turn(ledger, i, with_result=with_result)
        current = [_fingerprint(m) for m in rendered[:-1]]
        changed = [idx for idx, fp in enumerate(previous) if current[idx] != fp]
        previous = current

        if changed:
            for turn, anchor in placed:
                assert anchor < min(changed), (turn, anchor, i, changed)
            checked += len(placed)

        marked = apply_cache_breakpoints(rendered, horizon_depths=horizon)
        indexes = _breakpoint_indexes(marked)
        if len(indexes) < 2:
            continue
        for index in indexes[1:]:
            assert index < len(rendered) - 1, "never the live tail"
            placed.append((i, index))

    assert checked >= 5, "the transcript anchor never engaged; the replay is too short"


def test_thirteen_planning_turns_now_get_a_transcript_breakpoint():
    """The regression this module exists for.

    Thirteen operator turns at the production settings is what the measured
    run did. The old rule needed nineteen human messages before it would place
    anything; a turn that never reached a terminal action contributes one, so
    that run got no transcript breakpoint at all and its cached prefix
    plateaued at the system prompt while the transcript grew past it.
    """
    messages = _replay(13, REAL_SCRUB_DEPTH, with_result=False)

    anchors = transcript_anchors(messages, REAL_HORIZON)

    assert _old_stable_prefix_index(messages, REAL_HORIZON) is None, "the bug, reproduced"
    assert len(messages) == 38
    assert anchors == [12, 24]
    assert anchors[-1] < len(messages) - 4, "well before the live tail"
    assert _breakpoint_indexes(apply_cache_breakpoints(messages, horizon_depths=REAL_HORIZON)) == [
        0,
        12,
        24,
    ]


def test_the_anchor_is_far_newer_than_the_rule_it_replaced():
    """Where the old rule did fire, it was pinned needlessly far back.

    With a terminal action every turn the same thirteen turns carry twice the
    human messages, so the old count-based rule placed something — but at a
    position derived from a message count rather than from what the scrub edge
    has actually finished with, nineteen messages behind the tail regardless.
    """
    messages = _replay(13, REAL_SCRUB_DEPTH)

    old = _old_stable_prefix_index(messages, REAL_HORIZON)
    anchors = transcript_anchors(messages, REAL_HORIZON)

    assert old == 13
    assert anchors == [16, 32]
    assert anchors[-1] > old, "the exact anchor caches strictly more than the old rule did"


def test_the_anchors_move_forward_as_turns_accumulate():
    """The cached prefix grows with the conversation instead of plateauing."""
    ledger = _fresh_ledger(REAL_SCRUB_DEPTH)

    exact: list[int] = []
    for i in range(1, 21):
        rendered = _play_turn(ledger, i)
        index = frozen_prefix_index(rendered)
        if index is not None:
            exact.append(index)

    assert exact == sorted(exact), f"the frontier went backwards: {exact}"
    assert exact[-1] > exact[0], "the anchor never advanced"
    assert len(set(exact)) >= 5


def test_short_replay_gets_no_transcript_breakpoint():
    """Three turns is fewer observations than the horizon: system only."""
    messages = _replay(3, REAL_SCRUB_DEPTH)

    marked = apply_cache_breakpoints(messages, horizon_depths=REAL_HORIZON)

    assert _breakpoint_indexes(marked) == [0]
    assert horizon_prefix_index(messages, REAL_HORIZON) is None
    assert stable_prefix_index(messages, REAL_HORIZON) is None


def test_short_conversation_gets_no_transcript_breakpoint():
    """Plain string turns carry no observation: system only, no crash."""
    messages = [SystemMessage(content="STATIC")]
    messages += [HumanMessage(content=f"turn {i}") for i in range(2 * HORIZON)]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)

    assert _breakpoint_indexes(marked) == [0]
    assert stable_prefix_index(messages, HORIZON) is None


def test_messages_the_predicates_do_not_recognise_place_nothing():
    """Unknown block shapes are ambiguous, and ambiguity means no breakpoint.

    Neither derivation can read these: nothing is rewritable, so there is no
    frontier, and nothing is an observation, so there is no depth ladder. The
    request keeps the system breakpoint and no transcript one.
    """
    messages: list = [SystemMessage(content="STATIC")]
    for i in range(40):
        messages.append(HumanMessage(content=[{"type": "video", "url": f"v{i}"}]))
        messages.append(AIMessage(content=[{"type": "thinking", "thinking": f"t{i}"}]))

    assert frozen_prefix_index(messages) is None
    assert horizon_prefix_index(messages, HORIZON) is None
    assert transcript_anchors(messages, HORIZON) == []
    assert _breakpoint_indexes(apply_cache_breakpoints(messages, horizon_depths=HORIZON)) == [0]


def test_only_one_derivation_applying_places_nothing():
    """Rewritable messages with no observation among them: still no anchor."""
    messages: list = [SystemMessage(content="STATIC")]
    messages += [
        HumanMessage(content=[{"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n[1] button {i}"}])
        for i in range(40)
    ]

    assert frozen_prefix_index(messages) == 0
    assert horizon_prefix_index(messages, HORIZON) is None
    assert transcript_anchors(messages, HORIZON) == []


def test_the_shared_predicate_agrees_with_the_compressor_it_came_from():
    """A message the predicate calls frozen is never rewritten again.

    :func:`has_pending_scrub_edits` is the extracted form of the three gates
    in ``ScrubEdgeCompressor._rewrite``. This drives the compressor itself and
    holds it to that reading, so the two cannot drift apart.
    """
    compressor = ScrubEdgeCompressor(
        _ready_service(40),
        image_scrub_depth=REAL_SCRUB_DEPTH,
        strip_markers=(PRO_UI_LIST_MARKER, PLAN_RECITATION_MARKER),
        summary_key_getter=lambda m: m.additional_kwargs.get("step_key"),
    )

    messages: list = []
    settled: dict[int, str] = {}
    for i in range(1, 21):
        observation = _observation(i)
        observation.additional_kwargs["step_key"] = f"step-{i}"
        messages.append(observation)
        messages.append(AIMessage(content=f"thinking {i}"))
        compressor.compress(messages)

        for index, message in enumerate(messages):
            fingerprint = _fingerprint(message)
            if index in settled:
                assert fingerprint == settled[index], (i, index)
            elif not has_pending_scrub_edits(message):
                settled[index] = fingerprint

    observations = [index for index in settled if index % 2 == 0]
    assert len(observations) >= 5, "no observation ever settled; the replay proves nothing"
    assert all(is_screenshot_observation(messages[index]) for index in observations), (
        "a resolved observation stopped reading as one, so the depth count would drift"
    )


def test_no_system_prompt_still_works():
    """A messages-only request gets the transcript anchors and does not crash."""
    messages = _replay(16)[1:]

    marked = apply_cache_breakpoints(messages, horizon_depths=HORIZON)
    indexes = _breakpoint_indexes(marked)

    assert indexes == transcript_anchors(messages, HORIZON)
    assert len(indexes) == 2
    assert indexes[-1] < len(messages) - 1


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
    """A real replayed transcript reaches the provider with all three anchors."""
    wrapper, base = _wrapped(ModelProvider.ANTHROPIC)
    messages = _replay(16, REAL_SCRUB_DEPTH)

    await wrapper.ainvoke(messages)

    indexes = _breakpoint_indexes(base.seen)
    assert base.seen is not messages
    assert indexes[0] == 0, "the system anchor"
    assert len(indexes) == 3
    assert _breakpoint_indexes(messages) == []
