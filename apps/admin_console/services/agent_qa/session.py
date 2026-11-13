"""Input model: one stored check session (per theme: steps, logcat, text nodes)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

ACTION_TYPES = ("tap", "long_press", "swipe", "back", "type", "launch")
SOURCES = ("persona", "route")


@dataclass(frozen=True, slots=True)
class Step:
    step: int
    theme: str
    timestamp: str
    screen_before: str
    screen_after: str
    action: dict[str, Any]
    before_png: str
    after_png: str
    source: str = "persona"
    text_nodes: tuple[dict[str, Any], ...] = ()
    flag_secure: bool = False


@dataclass(frozen=True, slots=True)
class ThemeRun:
    steps: tuple[Step, ...]
    logcat_crash: str = ""
    logcat: str = ""


@dataclass(frozen=True, slots=True)
class Session:
    package: str
    declared_screens: tuple[dict[str, str], ...]
    themes: dict[str, ThemeRun] = field(default_factory=dict)
    base_dir: Path = Path(".")
    first_run: dict[str, Any] | None = None
    status_bar_bounds: tuple[int, ...] = ()
    navigation_bar_bounds: tuple[int, ...] = ()


def load_session(directory: Path) -> Session:
    """Read ``session.json`` from ``directory``; PNG paths in it are relative to it."""
    raw = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    themes = {
        name: ThemeRun(
            steps=tuple(
                Step(**{**s, "text_nodes": tuple(s.get("text_nodes", ()))}) for s in run["steps"]
            ),
            logcat_crash=run.get("logcat_crash", ""),
            logcat=run.get("logcat", ""),
        )
        for name, run in raw["themes"].items()
    }
    return Session(
        package=raw["package"],
        declared_screens=tuple(raw["declared_screens"]),
        themes=themes,
        base_dir=directory,
        first_run=raw.get("first_run"),
        status_bar_bounds=tuple(raw.get("status_bar_bounds") or ()),
        navigation_bar_bounds=tuple(raw.get("navigation_bar_bounds") or ()),
    )
