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

"""Unit tests for the pure reaction-time aggregation (CHE-645).

All fixtures are synthetic ``StepRecord``/``TraceRecord`` lists — no DB
access, matching ``compute_step_reaction_time`` /
``compute_session_reaction_time``'s "pure aggregation" contract.
"""

from artemis.data_engine.models import StepRecord, TraceRecord
from artemis.data_engine.reaction_time import (
    ACTION_TRACE_TYPE,
    OperatorIterationOutcome,
    ReactionPhase,
    compute_session_reaction_time,
    compute_step_reaction_time,
)

SESSION_ID = "sess-1"


def _step(step_id: str, step_number: int, timestamp: float) -> StepRecord:
    return StepRecord(
        step_id=step_id,
        session_id=SESSION_ID,
        step_number=step_number,
        timestamp=timestamp,
    )


def _trace(
    name: str,
    *,
    trace_id: str,
    step_id: str,
    type_: str = "span",
    timestamp: float,
    duration: float | None,
    status: str = "success",
    payload: dict | None = None,
) -> TraceRecord:
    return TraceRecord(
        trace_id=trace_id,
        session_id=SESSION_ID,
        step_id=step_id,
        type=type_,
        name=name,
        timestamp=timestamp,
        duration=duration,
        status=status,
        payload=payload or {},
    )


def _iteration(
    step_id: str, trace_id: str, *, iteration: int, outcome: str, start: float, duration: float
) -> TraceRecord:
    return _trace(
        ReactionPhase.OPERATOR_ITERATION.value,
        trace_id=trace_id,
        step_id=step_id,
        timestamp=start + duration,
        duration=duration,
        payload={"iteration": iteration, "outcome": outcome},
    )


class TestComputeStepReactionTime:
    def test_normal_step_perception_to_action(self):
        step = _step("s1", 0, timestamp=999.0)
        traces = [
            _trace(
                ReactionPhase.PERCEPTION.value,
                trace_id="t1",
                step_id="s1",
                timestamp=102.0,
                duration=2.0,
            ),
            _trace(
                "tap",
                trace_id="t2",
                step_id="s1",
                type_=ACTION_TRACE_TYPE,
                timestamp=103.0,
                duration=0.5,
            ),
        ]
        result = compute_step_reaction_time(step, traces)

        assert result.reaction_time_s == 3.0  # 103.0 - 100.0
        assert result.reaction_time_source == "perception_to_action"
        assert result.phase_breakdown[ReactionPhase.PERCEPTION.value] == 2.0
        assert result.phase_breakdown[ACTION_TRACE_TYPE] == 0.5
        assert result.operator_iterations == 0
        assert result.bounce_count == 0
        assert result.bounce_time_s == 0.0
        assert result.outcomes == []

    def test_step_with_bounces_counts_and_sums_bounce_time(self):
        step = _step("s2", 1, timestamp=50.0)
        traces = [
            _iteration(
                "s2",
                "i1",
                iteration=0,
                outcome=OperatorIterationOutcome.PLAN_LEDGER_GATE,
                start=10.0,
                duration=1.0,
            ),
            _iteration(
                "s2",
                "i2",
                iteration=1,
                outcome=OperatorIterationOutcome.BURST_LIMIT,
                start=11.0,
                duration=0.5,
            ),
            _iteration(
                "s2",
                "i3",
                iteration=2,
                outcome=OperatorIterationOutcome.EXECUTED,
                start=11.5,
                duration=0.25,
            ),
        ]
        result = compute_step_reaction_time(step, traces)

        assert result.operator_iterations == 3
        assert result.bounce_count == 2
        assert result.bounce_time_s == 1.5
        assert result.outcomes == [
            OperatorIterationOutcome.PLAN_LEDGER_GATE.value,
            OperatorIterationOutcome.BURST_LIMIT.value,
            OperatorIterationOutcome.EXECUTED.value,
        ]

    def test_step_missing_perception_falls_back_to_earliest_trace(self):
        step = _step("s3", 2, timestamp=50.0)
        traces = [
            _trace(
                ReactionPhase.PRECONDITION_XML.value,
                trace_id="t1",
                step_id="s3",
                timestamp=20.0,
                duration=1.0,
            ),
            _trace(
                "tap",
                trace_id="t2",
                step_id="s3",
                type_=ACTION_TRACE_TYPE,
                timestamp=22.0,
                duration=0.5,
            ),
        ]
        result = compute_step_reaction_time(step, traces)

        # earliest trace start = 20.0 - 1.0 = 19.0; end = last action end = 22.0
        assert result.reaction_time_s == 3.0
        assert result.reaction_time_source == "earliest_trace_to_action"

    def test_step_missing_action_falls_back_to_step_timestamp(self):
        step = _step("s4", 3, timestamp=105.0)
        traces = [
            _trace(
                ReactionPhase.PERCEPTION.value,
                trace_id="t1",
                step_id="s4",
                timestamp=102.0,
                duration=2.0,
            ),
        ]
        result = compute_step_reaction_time(step, traces)

        assert result.reaction_time_s == 5.0  # 105.0 - 100.0
        assert result.reaction_time_source == "perception_to_step_record"

    def test_step_with_no_traces_at_all_is_unavailable(self):
        step = _step("s5", 4, timestamp=0.0)
        result = compute_step_reaction_time(step, [])

        assert result.reaction_time_s is None
        assert result.reaction_time_source == "unavailable"
        assert result.phase_breakdown == {}
        assert result.operator_iterations == 0

    def test_running_traces_excluded(self):
        step = _step("s6", 5, timestamp=999.0)
        traces = [
            _trace(
                ReactionPhase.PERCEPTION.value,
                trace_id="t1",
                step_id="s6",
                timestamp=102.0,
                duration=2.0,
                status="running",
            ),
        ]
        result = compute_step_reaction_time(step, traces)

        # The "running" row must not be counted as a completed perception span.
        assert result.phase_breakdown == {}
        assert result.reaction_time_source == "unavailable"


class TestComputeSessionReactionTime:
    def test_empty_session_degrades_gracefully(self):
        result = compute_session_reaction_time(SESSION_ID, [])

        assert result.step_count == 0
        assert result.steps_with_reaction_time == 0
        assert result.p50_reaction_time_s is None
        assert result.p90_reaction_time_s is None
        assert result.median_operator_iterations is None
        assert result.pct_steps_with_bounce == 0.0
        assert result.total_reaction_time_s == 0.0
        assert result.total_bounce_time_s == 0.0
        assert result.bounce_share_of_reaction_time is None
        assert result.bounce_histogram == {}
        assert result.steps == []

    def test_percentiles_and_bounce_share(self):
        # Five steps with reaction times 1, 2, 3, 4, 5 seconds via
        # perception (start=0) -> action (end=N).
        steps_with_traces = []
        for i, reaction_time in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
            step_id = f"s{i}"
            step = _step(step_id, i, timestamp=0.0)
            traces = [
                _trace(
                    ReactionPhase.PERCEPTION.value,
                    trace_id=f"p{i}",
                    step_id=step_id,
                    timestamp=0.0,
                    duration=0.0,
                ),
                _trace(
                    "tap",
                    trace_id=f"a{i}",
                    step_id=step_id,
                    type_=ACTION_TRACE_TYPE,
                    timestamp=reaction_time,
                    duration=0.1,
                ),
            ]
            steps_with_traces.append((step, traces))

        result = compute_session_reaction_time(SESSION_ID, steps_with_traces)

        assert result.step_count == 5
        assert result.steps_with_reaction_time == 5
        assert result.p50_reaction_time_s == 3.0
        assert result.p90_reaction_time_s == 4.6
        assert result.total_reaction_time_s == 15.0

    def test_bounce_percentage_and_histogram(self):
        step_a = _step("sa", 0, timestamp=100.0)
        traces_a = [
            _iteration(
                "sa",
                "ia1",
                iteration=0,
                outcome=OperatorIterationOutcome.PLAN_LEDGER_GATE,
                start=0.0,
                duration=2.0,
            ),
            _iteration(
                "sa",
                "ia2",
                iteration=1,
                outcome=OperatorIterationOutcome.EXECUTED,
                start=2.0,
                duration=1.0,
            ),
        ]
        step_b = _step("sb", 1, timestamp=100.0)
        traces_b = [
            _iteration(
                "sb",
                "ib1",
                iteration=0,
                outcome=OperatorIterationOutcome.NO_TOOL_CALL,
                start=0.0,
                duration=0.5,
            ),
        ]

        result = compute_session_reaction_time(SESSION_ID, [(step_a, traces_a), (step_b, traces_b)])

        assert result.pct_steps_with_bounce == 50.0  # only step "sa" has a bounce
        assert result.total_bounce_time_s == 2.0
        assert result.bounce_histogram == {
            OperatorIterationOutcome.PLAN_LEDGER_GATE.value: 1,
            OperatorIterationOutcome.EXECUTED.value: 1,
            OperatorIterationOutcome.NO_TOOL_CALL.value: 1,
        }

    def test_bounce_share_of_reaction_time_arithmetic(self):
        step = _step("sc", 0, timestamp=999.0)
        traces = [
            _trace(
                ReactionPhase.PERCEPTION.value,
                trace_id="tp",
                step_id="sc",
                timestamp=0.0,
                duration=0.0,
            ),
            _trace(
                "tap",
                trace_id="ta",
                step_id="sc",
                type_=ACTION_TRACE_TYPE,
                timestamp=10.0,
                duration=0.1,
            ),
            _iteration(
                "sc",
                "ib1",
                iteration=0,
                outcome=OperatorIterationOutcome.BURST_LIMIT,
                start=0.0,
                duration=4.0,
            ),
        ]
        result = compute_session_reaction_time(SESSION_ID, [(step, traces)])

        assert result.total_reaction_time_s == 10.0
        assert result.total_bounce_time_s == 4.0
        assert result.bounce_share_of_reaction_time == 0.4

    def test_median_operator_iterations(self):
        steps_with_traces = []
        for i, n_iters in enumerate([1, 3, 5]):
            step_id = f"m{i}"
            step = _step(step_id, i, timestamp=0.0)
            traces = [
                _iteration(
                    step_id,
                    f"m{i}i{j}",
                    iteration=j,
                    outcome=OperatorIterationOutcome.EXECUTED,
                    start=float(j),
                    duration=0.1,
                )
                for j in range(n_iters)
            ]
            steps_with_traces.append((step, traces))

        result = compute_session_reaction_time(SESSION_ID, steps_with_traces)

        assert result.median_operator_iterations == 3.0
