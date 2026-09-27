"""Shared move/state/gate contract for live fast-lane decisions and offline replay."""

import asyncio
from dataclasses import dataclass
import json
import math
import re
import time
from typing import Any

from artemis.services.jev import (
    ChoiceAnswer,
    DEFAULT_TIMEOUT_SECONDS,
    JevClient,
    ask,
    choice_question,
)
from artemis.utils.plan_grammar import parse_plan


RISKY_LABEL = re.compile(
    r"\b(?:delete|remove|pay|buy|purchase|send|submit|confirm|uninstall|reset|erase|"
    r"sign[\s_-]+out|log[\s_-]+out)\b",
    re.IGNORECASE,
)
SCROLL_DIRECTIONS = {"scroll_down_reveal_below": "up", "scroll_up_reveal_above": "down"}


@dataclass(frozen=True)
class GateContext:
    mode: str
    moves: dict[str, str]
    indexed_elements: list[dict]
    steps: list[dict]
    threshold: float = 0.9
    max_streak: int = 3


@dataclass(frozen=True)
class Decision:
    taken: bool
    reason: str


def build_moves(indexed_elements: list[dict], launchable_apps: dict[str, str]) -> dict[str, str]:
    moves = {
        f"tap_{element['index']}": f"Tap {str(element['text']).strip()}"
        for element in indexed_elements
        if str(element.get("text") or "").strip()
    }
    moves.update(
        {
            "scroll_down_reveal_below": "Scroll DOWN to reveal items further below",
            "scroll_up_reveal_above": "Scroll UP to reveal items further above",
            "press_back": "Press the Back button",
            "press_home": "Press the Home button",
            **{f"launch_{app.lower()}": f"Launch the {app} app" for app in launchable_apps},
            "escalate": "None of these moves clearly progresses the milestone; hand off to the full planner",
        }
    )
    return moves


def build_state(
    milestone: str, foreground_app: str | None, screen_text: list[str], recent_actions: list[Any]
) -> str:
    return json.dumps(
        {
            "current_milestone": milestone,
            "foreground_app": foreground_app,
            "visible_screen_text": screen_text[:60],
            "recent_actions": recent_actions[-4:],
        },
        ensure_ascii=False,
    )


def milestone_text(task_plan: str) -> str:
    plan = parse_plan(task_plan)
    milestone = plan.active_milestone()
    if milestone is None:
        return ""
    leaf = plan.active_leaf(milestone)
    return milestone.text + (f"\n{leaf.text}" if leaf else "")


def launchable_apps(milestone: str, package_cache: dict[str, str | None]) -> dict[str, str]:
    return {
        app: package
        for app, package in package_cache.items()
        if package and app and re.search(rf"(?<!\w){re.escape(app)}(?!\w)", milestone, re.I)
    }


def jev_streak(steps: list[dict]) -> int:
    streak = 0
    for step in reversed(steps):
        if (step.get("extra_metadata") or {}).get("decision_source") != "jev":
            break
        streak += 1
    return streak


def gate(answer: ChoiceAnswer | None, context: GateContext) -> Decision:
    if context.mode != "on":
        return Decision(False, "mode")
    if not isinstance(answer, ChoiceAnswer):
        return Decision(False, "malformed_answer")
    confidence = answer.confidence
    if (
        not isinstance(confidence, (float, int))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        return Decision(False, "malformed_answer")
    choice = answer.choice
    if choice == "escalate":
        return Decision(False, "escalate")
    if choice not in context.moves:
        return Decision(False, "unknown_move")
    if confidence < context.threshold:
        return Decision(False, "low_confidence")
    if choice.startswith("tap_"):
        element = next(
            (
                element
                for element in context.indexed_elements
                if f"tap_{element['index']}" == choice
            ),
            {},
        )
        label = str(element.get("text") or "").strip()
        if not label or not any(character.isalnum() for character in label):
            return Decision(False, "unlabelled")
        if RISKY_LABEL.search(label):
            return Decision(False, "risky_label")
        letters = [character for character in label if character.isalpha()]
        if sum(not character.isascii() for character in letters) / max(1, len(letters)) > 0.2:
            return Decision(False, "non_ascii_label")
    if choice in SCROLL_DIRECTIONS:
        actions = [action for step in context.steps for action in (step.get("action_taken") or [])]
        previous = actions[-1] if actions else {}
        if (
            previous.get("action") != "swipe"
            or previous.get("direction") != SCROLL_DIRECTIONS[choice]
        ):
            return Decision(False, "scroll_continuation")
    if jev_streak(context.steps) >= context.max_streak:
        return Decision(False, "streak")
    return Decision(True, "accepted")


def move_tool_call(choice: str, apps: dict[str, str]) -> dict:
    if choice.startswith("tap_"):
        return {"name": "click", "args": {"target": int(choice.removeprefix("tap_"))}}
    if choice in SCROLL_DIRECTIONS:
        return {"name": "swipe", "args": {"direction": SCROLL_DIRECTIONS[choice]}}
    if choice in ("press_back", "press_home"):
        return {"name": "press_key", "args": {"key": choice.removeprefix("press_").upper()}}
    app = next(app for app in apps if f"launch_{app.lower()}" == choice)
    return {"name": "manage_app", "args": {"action": "launch", "app_name": app}}


class FastLaneTurn:
    """Own one bounded request; a shadow request never holds up the frontier."""

    def __init__(
        self,
        client: JevClient | None,
        context: GateContext,
        state: str,
        apps: dict[str, str],
        model: str,
    ):
        self.client = client
        self.context = context
        self.state = state
        self.apps = apps
        self.model = model
        self.answer: ChoiceAnswer | None = None
        self.decision = Decision(False, "unconfigured" if client is None else "pending")
        self.latency_s = 0.0
        self.task: asyncio.Task | None = None

    async def query(self) -> None:
        started = time.monotonic()
        try:
            if self.client is None:
                return
            questions = {
                "next_move": choice_question(
                    "Choose the single next move that progresses the current milestone. "
                    "If the next move needs text input, completion, recovery, or is unclear, escalate.",
                    self.context.moves,
                )
            }
            async with asyncio.timeout(DEFAULT_TIMEOUT_SECONDS):
                answers = await ask(self.client, self.state, questions)
            candidate = (answers or {}).get("next_move")
            self.answer = candidate if isinstance(candidate, ChoiceAnswer) else None
            self.decision = gate(self.answer, self.context)
        except TimeoutError:
            self.decision = Decision(False, "timeout")
        except (ValueError, TypeError, KeyError):
            self.decision = Decision(False, "malformed_answer")
        finally:
            self.latency_s = time.monotonic() - started

    def start_shadow(self) -> None:
        self.task = asyncio.create_task(self.query())

    async def finish_shadow(self) -> None:
        if self.task is None:
            return
        if not self.task.done():
            self.decision = Decision(False, "frontier_finished")
            self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise

    def record(self, ctx: Any) -> None:
        if ctx.data_engine:
            answer = self.answer
            ctx.data_engine.record_trace(
                type="jev",
                name="fast_lane",
                step_id=getattr(ctx.data_engine, "current_step_id", None),
                duration=self.latency_s,
                payload={
                    "mode": self.context.mode,
                    "choice": answer.choice if answer else None,
                    "confidence": answer.confidence if answer else None,
                    "probabilities_top3": dict(
                        sorted(
                            answer.probabilities.items(), key=lambda item: item[1], reverse=True
                        )[:3]
                    )
                    if answer
                    else {},
                    "gate_result": self.decision.taken,
                    "gate_reason": self.decision.reason,
                    "latency_s": self.latency_s,
                    "taken": self.decision.taken,
                    "move_count": len(self.context.moves),
                    "launchable_apps": self.apps,
                    "model": self.model,
                },
            )
