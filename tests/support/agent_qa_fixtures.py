"""Build stored check sessions on disk for agent QA report tests (no device)."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

PACKAGE = "com.example.sample"
SCREENS = ["Home", "NativePool", "Pager"]
# step: (screen_before, screen_after, action)
ROUTE = [
    ("Home", "Home", {"type": "launch", "target": {}}),
    (
        "Home",
        "NativePool",
        {"type": "tap", "target": {"text": "native pool", "bounds": [0, 0, 10, 10]}},
    ),
    ("NativePool", "Home", {"type": "back", "target": {}}),
    ("Home", "Pager", {"type": "tap", "target": {"text": "pager", "bounds": [0, 0, 10, 10]}}),
    ("Pager", "Pager", {"type": "swipe", "target": {"bounds": [0, 0, 10, 10]}}),
    ("Pager", "Home", {"type": "back", "target": {}}),
]


def write_png(
    path: Path, *, blank: bool = False, bg=(255, 255, 255), fg=(0, 0, 0), size=(120, 60)
) -> None:
    img = Image.new("RGB", size, bg)
    if not blank:
        ImageDraw.Draw(img).rectangle([10, 10, 60, 30], fill=fg)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _theme_steps(root: Path, theme: str, route=ROUTE, source="persona") -> list[dict]:
    bg = (255, 255, 255) if theme == "light" else (18, 18, 18)
    fg = (0, 0, 0) if theme == "light" else (240, 240, 240)
    steps = []
    for n, (before, after, action) in enumerate(route, start=1):
        for side in ("before", "after"):
            write_png(root / theme / f"{n}-{side}.png", bg=bg, fg=fg)
        steps.append(
            {
                "step": n,
                "theme": theme,
                "timestamp": f"2026-10-28T10:00:{n:02d}+07:00",
                "screen_before": before,
                "screen_after": after,
                "action": action,
                "before_png": f"{theme}/{n}-before.png",
                "after_png": f"{theme}/{n}-after.png",
                "source": source,
            }
        )
    return steps


def write_session(root: Path, *, themes=("light", "dark"), route=ROUTE, **overrides) -> Path:
    """Write ``session.json`` plus PNGs under ``root``; ``overrides`` replace top-level keys."""
    data = {
        "package": PACKAGE,
        "declared_screens": [{"id": s, "purpose": f"{s} purpose"} for s in SCREENS],
        "themes": {
            t: {"steps": _theme_steps(root, t, route), "logcat_crash": "", "logcat": ""}
            for t in themes
        },
    }
    data.update(overrides)
    root.mkdir(parents=True, exist_ok=True)
    (root / "session.json").write_text(json.dumps(data), encoding="utf-8")
    return root
