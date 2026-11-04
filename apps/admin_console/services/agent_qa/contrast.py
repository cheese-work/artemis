"""WCAG 2.2 AA contrast for text nodes, from a11y bounds plus pixel samples."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
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


MIN_CORE_PX = 4
"""A core colour needs this many pixels, so a stray pixel is never read as text."""


def _neighbour_max(a: np.ndarray) -> np.ndarray:
    """Per pixel, the largest value among its 8 neighbours (edges padded with -inf)."""
    padded = np.pad(a, 1, constant_values=-np.inf)
    h, w = a.shape
    shifted = (
        padded[dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3) if (dy, dx) != (1, 1)
    )
    return np.maximum.reduce(list(shifted))


def measure_node(png: Path, node: dict[str, Any]) -> float | None:
    """Worst ratio between the node's background and any text-core colour in its bounds.

    The background is the commonest exact colour. Every other pixel is foreground:
    nothing is dropped by share or colour bucket. A *core* pixel is a foreground
    local maximum of contrast (no neighbour is farther from the background), so
    the anti-aliasing ramp around a stroke is excluded by position, not by size.
    The result is the *minimum* over core colours of at least ``MIN_CORE_PX`` pixels:
    a border, icon or other non-text shape only adds a core colour and cannot lift
    the result. The failure mode is a false red, never a false green.

    ``None`` when no core colour exists (a single-colour crop): the caller must
    treat declared text it cannot measure as not readable. Raises ``ValueError`` for
    bounds that are malformed, empty or outside the image.
    """
    try:
        left, top, right, bottom = (int(v) for v in node["bounds"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("bounds_invalid") from err
    with Image.open(png) as img:
        rgb = img.convert("RGB")
        if not (0 <= left < right <= rgb.width and 0 <= top < bottom <= rgb.height):
            raise ValueError("bounds_invalid")
        crop = np.asarray(rgb.crop((left, top, right, bottom)), dtype=np.uint32)
    packed = (crop[..., 0] << 16) | (crop[..., 1] << 8) | crop[..., 2]
    colours, inverse, counts = np.unique(packed, return_inverse=True, return_counts=True)
    bg = colours[counts.argmax()]
    bg_rgb = ((bg >> 16) & 255, (bg >> 8) & 255, bg & 255)
    ratio_of = np.array(
        [
            contrast_ratio(((c >> 16) & 255, (c >> 8) & 255, c & 255), bg_rgb)  # type: ignore[arg-type]
            for c in colours.tolist()
        ]
    )
    ratio = ratio_of[inverse.reshape(packed.shape)]
    core = (packed != bg) & (ratio >= _neighbour_max(ratio))
    core_colours, core_counts = np.unique(packed[core], return_counts=True)
    keep = core_counts >= MIN_CORE_PX
    if not keep.any():
        return None
    return float(ratio_of[np.searchsorted(colours, core_colours[keep])].min())
