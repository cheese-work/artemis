"""Flow artifact: ordered steps per theme, validation and screen map."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from PIL import Image, UnidentifiedImageError

from apps.admin_console.services.agent_qa.session import Session


class FlowError(Exception):
    """``code`` is the stable machine string, e.g. ``flow_mismatch at step 3``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Flow:
    theme: str
    steps: tuple[dict, ...]
    screen_map: dict[str, list[dict]]


def _real_png(path: Path) -> bool:
    """A readable image with more than one colour (an all-one-colour frame is blank)."""
    try:
        with Image.open(path) as img:
            bands = cast(tuple[tuple[int, int], ...], img.convert("RGB").getextrema())
    except (OSError, UnidentifiedImageError):
        return False
    return any(lo != hi for lo, hi in bands)


def build_flow(session: Session, theme: str) -> Flow:
    """Validate one theme's steps and derive the screen map.

    Raises ``FlowError("flow_incomplete")`` for an empty flow, a step without an
    action, or a missing or blank Before/After PNG.
    """
    steps = session.themes[theme].steps
    if not steps:
        raise FlowError("flow_incomplete")
    out: list[dict] = []
    for expected, step in enumerate(steps, start=1):
        if step.step != expected or not step.action.get("type"):
            raise FlowError("flow_incomplete")
        if not all(_real_png(session.base_dir / png) for png in (step.before_png, step.after_png)):
            raise FlowError("flow_incomplete")
        out.append(
            {
                "step": step.step,
                "theme": step.theme,
                "timestamp": step.timestamp,
                "screen_before": step.screen_before,
                "screen_after": step.screen_after,
                "action": step.action,
                "before_png": step.before_png,
                "after_png": step.after_png,
                "source": step.source,
            }
        )
    screen_map: dict[str, list[dict]] = {}
    for step in out:
        screen_map.setdefault(step["screen_before"], [])
        screen_map.setdefault(step["screen_after"], [])
        if step["screen_before"] != step["screen_after"]:
            leave = {"to": step["screen_after"], "action": step["action"]["type"]}
            if leave not in screen_map[step["screen_before"]]:
                screen_map[step["screen_before"]].append(leave)
    return Flow(theme, tuple(out), screen_map)


def verify_route(flow: Flow, expected_screens: list[str]) -> None:
    """Each step's ``screen_after`` must equal the expected screen for that step."""
    for n, step in enumerate(flow.steps, start=1):
        if n > len(expected_screens) or step["screen_after"] != expected_screens[n - 1]:
            raise FlowError(f"flow_mismatch at step {n}")
    if len(expected_screens) > len(flow.steps):
        raise FlowError(f"flow_mismatch at step {len(flow.steps) + 1}")
