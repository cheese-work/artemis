"""Flow artifact: ordered steps per theme, validation and screen map."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
import re
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


_SECRET_PLACEHOLDER = re.compile(r"\$\{secret:[A-Za-z_][A-Za-z0-9_.-]*\}")
_TYPED_VALUE_KEYS = ("text", "value", "input", "content", "typed_text", "input_text")


def validate_route(route: list[dict]) -> None:
    """Refuse literal values in route steps marked as secret typing."""
    for action in route:
        if action.get("type") != "type" or action.get("secret") is not True:
            continue
        values = [action[key] for key in _TYPED_VALUE_KEYS if key in action]
        if not values or any(
            not isinstance(value, str) or not _SECRET_PLACEHOLDER.fullmatch(value)
            for value in values
        ):
            raise FlowError("route_secret_literal")


def _marked(value: object) -> bool:
    return value is True or value == "true"


def secret_values(session: Session) -> set[str]:
    """Return typed values whose action or accessibility node marks them private."""
    values = set()
    for run in session.themes.values():
        for step in run.steps:
            marked_nodes = [
                node
                for node in step.text_nodes
                if _marked(node.get("password")) or _marked(node.get("secret"))
            ]
            if step.action.get("secret") is True or marked_nodes:
                values.update(
                    value
                    for key in _TYPED_VALUE_KEYS
                    if isinstance((value := step.action.get(key)), str) and value
                )
            for node in marked_nodes:
                values.update(
                    value
                    for key in _TYPED_VALUE_KEYS
                    if isinstance((value := node.get(key)), str) and value
                )
    return values


def redact_known_values(value, values: set[str]):
    """Copy a JSON-shaped value while replacing known secret strings."""
    if isinstance(value, str):
        for secret in sorted(values, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {key: redact_known_values(item, values) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_known_values(item, values) for item in value)
    return value


def _length_class(value: str) -> str:
    if len(value) <= 8:
        return "short"
    if len(value) <= 16:
        return "medium"
    return "long"


def _private_action(action: dict, private: bool, values: set[str]) -> dict:
    result = deepcopy(action)
    if private:
        classes = []
        for key in _TYPED_VALUE_KEYS:
            value = result.get(key)
            if value is not None:
                if isinstance(value, str):
                    classes.append(_length_class(value))
                result[key] = "[redacted]"
        if classes:
            result["text_length_class"] = max(classes, key={"short": 0, "medium": 1, "long": 2}.get)
    return redact_known_values(result, values)


def _session_png(session: Session, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise FlowError("flow_incomplete")
    root = session.base_dir.resolve()
    candidate = (session.base_dir / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise FlowError("flow_incomplete") from exc
    return candidate


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
    private_values = secret_values(session)
    for expected, step in enumerate(steps, start=1):
        if step.step != expected or not step.action.get("type"):
            raise FlowError("flow_incomplete")
        images = [_session_png(session, png) for png in (step.before_png, step.after_png)]
        if not all(_real_png(path) for path in images):
            raise FlowError("flow_incomplete")
        private = step.action.get("secret") is True or any(
            _marked(node.get("password")) or _marked(node.get("secret")) for node in step.text_nodes
        )
        out.append(
            {
                "step": step.step,
                "theme": step.theme,
                "timestamp": step.timestamp,
                "screen_before": step.screen_before,
                "screen_after": step.screen_after,
                "action": _private_action(step.action, private, private_values),
                "before_png": step.before_png,
                "after_png": step.after_png,
                "source": step.source,
                **({"screenshot_label": "FLAG_SECURE"} if step.flag_secure else {}),
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
