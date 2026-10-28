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


STRAY_SHARE = 0.05
FRAME_SHARE = 0.9


def _peel_frame(crop: Image.Image) -> Image.Image:
    """Drop outer rings that are one colour (control outline, padding), so they are never read as text.

    Stops at the first ring that holds glyph pixels, and always keeps at least a 2 px core.
    """
    px = crop.load()
    left, top, right, bottom = 0, 0, crop.width, crop.height
    while right - left > 2 and bottom - top > 2 and px is not None:
        ring = [px[x, y] for x in range(left, right) for y in (top, bottom - 1)] + [
            px[x, y] for y in range(top + 1, bottom - 1) for x in (left, right - 1)
        ]
        if Counter(ring).most_common(1)[0][1] < FRAME_SHARE * len(ring):
            break
        left, top, right, bottom = left + 1, top + 1, right - 1, bottom - 1
    return crop.crop((left, top, right, bottom))


def measure_node(png: Path, node: dict[str, Any]) -> float | None:
    """Ratio between the node's background (commonest colour in its bounds) and its text.

    The text colour is the colour farthest from the background among those that
    cover at least ``STRAY_SHARE`` of the non-background pixels, so a stray pixel
    cannot stand in for the text. Uniform outer rings (a control's outline) are
    peeled first, so a border cannot stand in for the text either. ``None`` when the bounds hold one colour only:
    there is no text pixel to measure. Raises ``ValueError`` for bounds that are
    malformed, empty or outside the image: such a node is never measured on padding.
    """
    try:
        left, top, right, bottom = (int(v) for v in node["bounds"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("bounds_invalid") from err
    with Image.open(png) as img:
        rgb = img.convert("RGB")
        if not (0 <= left < right <= rgb.width and 0 <= top < bottom <= rgb.height):
            raise ValueError("bounds_invalid")
        raw = _peel_frame(rgb.crop((left, top, right, bottom))).tobytes()
    colours = Counter(zip(raw[0::3], raw[1::3], raw[2::3]))
    bg = colours.most_common(1)[0][0]
    if len(colours) == 1:
        return None
    non_bg = sum(colours.values()) - colours[bg]
    text = [c for c, n in colours.items() if c != bg and n >= STRAY_SHARE * non_bg]
    fg = max(text, key=lambda c: contrast_ratio(c, bg))
    return contrast_ratio(fg, bg)
