"""WCAG 2.2 AA contrast for text nodes, from a11y bounds plus pixel samples."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image

NORMAL_RATIO = 4.5
LARGE_RATIO = 3.0
LARGE_PT = 18
LARGE_BOLD_PT = 14

RGB = tuple[int, int, int]


def _luminance(rgb: RGB) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: RGB, bg: RGB) -> float:
    hi, lo = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def required_ratio(node: dict[str, Any]) -> float:
    """3:1 for text of at least 18 pt, or 14 pt bold; 4.5:1 otherwise (incl. unknown size)."""
    pt = node.get("size_pt") or 0
    large = pt >= LARGE_PT or (node.get("bold") and pt >= LARGE_BOLD_PT)
    return LARGE_RATIO if large else NORMAL_RATIO


def measure_node(png: Path, node: dict[str, Any]) -> float | None:
    """Ratio between the node's background (commonest colour in its bounds) and its text.

    The text colour is the sampled pixel farthest from the background. ``None``
    when the bounds hold one colour only: there is no text pixel to measure.
    """
    left, top, right, bottom = node["bounds"]
    with Image.open(png) as img:
        crop = img.convert("RGB").crop((left, top, right, bottom))
        raw = crop.tobytes()
    pixels = list(zip(raw[0::3], raw[1::3], raw[2::3]))
    if not pixels:
        return None
    colours = Counter(pixels)
    bg = colours.most_common(1)[0][0]
    if len(colours) == 1:
        return None
    fg = max(colours, key=lambda c: contrast_ratio(c, bg))
    return contrast_ratio(fg, bg)
