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

"""Reaction-time instrumentation: phase names, Operator iteration outcome
labels, and pure aggregation for "how long does one step take to decide an
action, and how much of that is the Operator re-asking itself?" (CHE-645).

This module owns:

1. The stable ``type="span"`` trace names (:class:`ReactionPhase`) and the
   :class:`OperatorIterationOutcome` labels — the single source of truth
   imported at every instrumentation call site (``graph/perception.py``,
   ``graph/graph.py``, ``agents/operator/operator.py``,
   ``agents/validator/precondition_xml.py``,
   ``agents/validator/precondition_pixel.py``). Spans themselves are written
   with :class:`artemis.data_engine.trace.PhaseSpan` /
   :func:`artemis.data_engine.trace.record_phase_span` — non-blocking,
   observation-only, no decision-path behaviour changes.
2. Pure aggregation (:func:`compute_step_reaction_time`,
   :func:`compute_session_reaction_time`) over already-fetched
   ``(StepRecord, list[TraceRecord])`` rows — no DB access, unit-testable.
3. A thin DB-reading entry point (:func:`read_session_reaction_time`) for the
   ``artemis trace timing`` CLI command.

Reaction time per step is defined as *last action-trace end minus perception
span start* (a completed trace's ``timestamp`` is always its *end*; start is
derived as ``timestamp - (duration or 0)`` — see
``data_engine/trace.py``). Both ends have documented fallbacks so a step
missing data (a flash profile has no safety net phases; a step may record no
action at all, e.g. a subgoal-review-only turn) degrades gracefully instead
of raising:

- **Start**: the ``phase:perception`` span's start, if one was recorded for
  the step. Otherwise the earliest start among all of the step's traces.
  Otherwise ``None`` (no traces at all for the step).
- **End**: the latest ``type="action"`` trace's end (already recorded by
  ``execution_loop.py`` — not duplicated here). Otherwise the step's own
  ``StepRecord.timestamp`` (stamped at ``execution_check_node``, the
  closest available end-of-turn marker when no action ran).

``reaction_time_s`` is ``None`` only when neither end has a usable value, or
when the computed end precedes the computed start (clock skew / corrupt
data) — never an exception.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
import math
from pathlib import Path
from statistics import median
from uuid import UUID

from pydantic import BaseModel

from artemis.data_engine.models import StepRecord, TraceRecord
from artemis.data_engine.storage import StorageManager


class ReactionPhase(StrEnum):
    """Stable ``type="span"`` trace names for the phases that make up one
    Operator decision step's wall clock.

    Action dispatch (``type="action"``, ``execution_loop.py:246-291``) is
    already recorded with duration and is intentionally *not* one of these
    — it is folded into the aggregation separately (see module docstring),
    not duplicated as a phase span.
    """

    PERCEPTION = "phase:perception"
    PLANNER_JOIN = "phase:planner_join"
    PRECONDITION_XML = "phase:precondition_xml"
    PRECONDITION_PIXEL = "phase:precondition_pixel"
    OPERATOR_ITERATION = "phase:operator_iteration"


ALL_PHASES: tuple[ReactionPhase, ...] = (
    ReactionPhase.PERCEPTION,
    ReactionPhase.PLANNER_JOIN,
    ReactionPhase.PRECONDITION_XML,
    ReactionPhase.PRECONDITION_PIXEL,
    ReactionPhase.OPERATOR_ITERATION,
)

# Not a phase span — the pre-existing action-dispatch trace type, folded
# into the phase breakdown under this key (see module docstring).
ACTION_TRACE_TYPE = "action"


class OperatorIterationOutcome(StrEnum):
    """Exact classification of why one Operator tool-loop iteration ended.

    Recorded once per iteration (``operator.py``'s ``_invoke_llm_loop``) as
    the ``phase:operator_iteration`` span's ``outcome`` payload field — the
    number CHE-645 exists for.
    """

    #: A terminal action was translated and accepted; the tool loop breaks.
    EXECUTED = "executed"
    #: The LLM returned no tool call at all; the turn ends with no decision.
    NO_TOOL_CALL = "no_tool_call"
    #: A helper/scratchpad tool call failed, so any accompanying screen
    #: actions were dropped for this turn (never sent for translation).
    HELPER_TOOL_FAILURE = "helper_tool_failure"
    #: A result-dependent "deferring" tool (memory/exploration) was called
    #: alongside terminal actions; the actions were deferred this turn.
    DEFERRING_TOOL_MIX = "deferring_tool_mix"
    #: The plan-ledger gate bounced a single action for lacking a matching
    #: plan write.
    PLAN_LEDGER_GATE = "plan_ledger_gate"
    #: The action batch exceeded the fast-action burst cap.
    BURST_LIMIT = "burst_limit"
    #: One or more actions failed per-action translation/validation
    #: (``_translate_and_validate_tool``) with no more specific label above.
    VALIDATION_ERRORS = "validation_errors"
    #: The tool loop exhausted every iteration without ever breaking (the
    #: ``for...else`` branch) — the very last iteration fell through with
    #: nothing decided.
    TOOL_LIMIT_EXCEEDED = "tool_limit_exceeded"
    #: Fallback for an iteration that called only non-terminal helper/
    #: deferring tools (no action calls at all) and simply loops again.
    #: Not one of CHE-645's enumerated labels, but needed so every
    #: iteration gets a precise, non-inferred outcome.
    CONTINUED = "continued"


#: Outcomes where the tool loop terminates on this iteration — the Operator
#: is *not* re-asking itself afterwards, either because it committed to an
#: action or because it gave up on this turn outright.
TERMINAL_OUTCOMES: frozenset[OperatorIterationOutcome] = frozenset(
    {OperatorIterationOutcome.EXECUTED, OperatorIterationOutcome.NO_TOOL_CALL}
)


def is_bounce(outcome: str) -> bool:
    """True when an iteration outcome is the Operator "re-asking itself" —
    i.e. every outcome except the two that terminate the loop."""
    return outcome not in TERMINAL_OUTCOMES


class StepReactionTime(BaseModel):
    """Reaction-time breakdown for a single step."""

    step_id: str
    step_number: int
    reaction_time_s: float | None
    reaction_time_source: str
    phase_breakdown: dict[str, float]
    operator_iterations: int
    bounce_count: int
    bounce_time_s: float
    outcomes: list[str]


class SessionReactionTime(BaseModel):
    """Reaction-time report for a whole session."""

    session_id: str
    step_count: int
    steps_with_reaction_time: int
    p50_reaction_time_s: float | None
    p90_reaction_time_s: float | None
    median_operator_iterations: float | None
    pct_steps_with_bounce: float
    total_reaction_time_s: float
    total_bounce_time_s: float
    bounce_share_of_reaction_time: float | None
    bounce_histogram: dict[str, int]
    steps: list[StepReactionTime]


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (numpy's default method). Pure,
    never raises — returns ``None`` for an empty input."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    lower_val = ordered[int(lower)] * (upper - rank)
    upper_val = ordered[int(upper)] * (rank - lower)
    return lower_val + upper_val


def _phase_spans(traces: list[TraceRecord], phase: ReactionPhase) -> list[TraceRecord]:
    return [t for t in traces if t.type == "span" and t.name == phase.value]


def _trace_start(trace: TraceRecord) -> float:
    """A completed trace's ``timestamp`` is its end; start is derived."""
    return trace.timestamp - (trace.duration or 0.0)


def compute_step_reaction_time(step: StepRecord, traces: list[TraceRecord]) -> StepReactionTime:
    """Pure aggregation of one step's reaction time from its trace rows.

    No DB access; never raises on missing/malformed data (see module
    docstring for the documented start/end fallbacks).
    """
    completed_traces = [t for t in traces if t.status != "running"]

    phase_breakdown: dict[str, float] = {}
    for phase in ALL_PHASES:
        spans = _phase_spans(completed_traces, phase)
        if spans:
            phase_breakdown[phase.value] = sum(t.duration or 0.0 for t in spans)

    action_traces = [t for t in completed_traces if t.type == ACTION_TRACE_TYPE]
    if action_traces:
        phase_breakdown[ACTION_TRACE_TYPE] = sum(t.duration or 0.0 for t in action_traces)

    perception_spans = _phase_spans(completed_traces, ReactionPhase.PERCEPTION)
    start_time: float | None = None
    if perception_spans:
        start_time = min(_trace_start(t) for t in perception_spans)
    elif completed_traces:
        start_time = min(_trace_start(t) for t in completed_traces)

    end_time: float | None = None
    if action_traces:
        end_time = max(t.timestamp for t in action_traces)
    elif step.timestamp:
        end_time = step.timestamp

    reaction_time_s: float | None = None
    reaction_time_source = "unavailable"
    if start_time is not None and end_time is not None and end_time >= start_time:
        reaction_time_s = end_time - start_time
        if perception_spans and action_traces:
            reaction_time_source = "perception_to_action"
        elif perception_spans:
            reaction_time_source = "perception_to_step_record"
        elif action_traces:
            reaction_time_source = "earliest_trace_to_action"
        else:
            reaction_time_source = "earliest_trace_to_step_record"

    iteration_spans = sorted(
        _phase_spans(completed_traces, ReactionPhase.OPERATOR_ITERATION),
        key=lambda t: t.payload.get("iteration", 0),
    )
    outcomes = [str(t.payload.get("outcome", "")) for t in iteration_spans]
    bounce_time_s = sum(
        (t.duration or 0.0) for t in iteration_spans if is_bounce(str(t.payload.get("outcome", "")))
    )

    return StepReactionTime(
        step_id=str(step.step_id),
        step_number=step.step_number,
        reaction_time_s=reaction_time_s,
        reaction_time_source=reaction_time_source,
        phase_breakdown=phase_breakdown,
        operator_iterations=len(iteration_spans),
        bounce_count=sum(1 for o in outcomes if is_bounce(o)),
        bounce_time_s=bounce_time_s,
        outcomes=outcomes,
    )


def compute_session_reaction_time(
    session_id: str, steps_with_traces: list[tuple[StepRecord, list[TraceRecord]]]
) -> SessionReactionTime:
    """Pure aggregation over a whole session's steps. Degrades gracefully
    to zeros/``None`` for an empty session — never raises."""
    steps = sorted(
        (compute_step_reaction_time(step, traces) for step, traces in steps_with_traces),
        key=lambda s: s.step_number,
    )

    reaction_times = [s.reaction_time_s for s in steps if s.reaction_time_s is not None]
    iteration_counts = [s.operator_iterations for s in steps]
    steps_with_bounce = sum(1 for s in steps if s.bounce_count > 0)
    total_reaction_time_s = sum(reaction_times) if reaction_times else 0.0
    total_bounce_time_s = sum(s.bounce_time_s for s in steps)

    histogram: Counter[str] = Counter()
    for s in steps:
        histogram.update(s.outcomes)

    return SessionReactionTime(
        session_id=session_id,
        step_count=len(steps),
        steps_with_reaction_time=len(reaction_times),
        p50_reaction_time_s=_percentile(reaction_times, 50),
        p90_reaction_time_s=_percentile(reaction_times, 90),
        median_operator_iterations=median(iteration_counts) if iteration_counts else None,
        pct_steps_with_bounce=(steps_with_bounce / len(steps) * 100.0) if steps else 0.0,
        total_reaction_time_s=total_reaction_time_s,
        total_bounce_time_s=total_bounce_time_s,
        bounce_share_of_reaction_time=(
            total_bounce_time_s / total_reaction_time_s if total_reaction_time_s > 0 else None
        ),
        bounce_histogram=dict(histogram),
        steps=steps,
    )


def read_session_reaction_time(
    db_path: str | Path, traces_dir: str | Path, session_id: UUID | str
) -> SessionReactionTime:
    """Thin DB-reading entry point for the ``artemis trace timing`` CLI.

    Opens the traces database read-only and aggregates one session's
    reaction time. Built from :meth:`StorageManager.get_steps` and
    :meth:`StorageManager.get_traces_for_step` — the same primitives
    :meth:`StorageManager.get_steps_with_traces` composes — kept separate
    here only because that helper's signature is UUID-only while session
    ids read from a traces directory name are plain strings. All actual
    computation is the pure :func:`compute_session_reaction_time` above.
    """
    storage = StorageManager(db_path, traces_dir, read_only=True)
    steps = storage.get_steps(session_id)
    steps_with_traces = [(step, storage.get_traces_for_step(step.step_id)) for step in steps]
    return compute_session_reaction_time(str(session_id), steps_with_traces)
