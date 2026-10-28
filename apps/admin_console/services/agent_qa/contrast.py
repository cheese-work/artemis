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


STRONG_SHARE = 0.01
QUANT_SHIFT = 5


def measure_node(png: Path, node: dict[str, Any]) -> float | None:
    """Worst ratio between the node's background and any strong non-background colour.

    The background is the commonest colour in the bounds. Colours are quantised
    (``QUANT_SHIFT``) into clusters; a cluster is strong when it holds at least
    ``STRONG_SHARE`` of the crop. The result is the *minimum* ratio over strong
    clusters, so a border, icon or any other non-text shape can only add a cluster
    and never lift the result: the failure mode is a false red, not a false green.
    Anti-aliasing blends and a stray pixel are too small to be strong clusters.
    ``None`` when no strong cluster exists: there is no text to measure. Raises
    ``ValueError`` for bounds that are malformed, empty or outside the image.
    """
    try:
        left, top, right, bottom = (int(v) for v in node["bounds"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("bounds_invalid") from err
    with Image.open(png) as img:
        rgb = img.convert("RGB")
        if not (0 <= left < right <= rgb.width and 0 <= top < bottom <= rgb.height):
            raise ValueError("bounds_invalid")
        raw = rgb.crop((left, top, right, bottom)).tobytes()
    pixels = list(zip(raw[0::3], raw[1::3], raw[2::3]))
    clusters: dict[RGB, list[RGB]] = {}
    for px in pixels:
        clusters.setdefault(
            (px[0] >> QUANT_SHIFT, px[1] >> QUANT_SHIFT, px[2] >> QUANT_SHIFT), []
        ).append(px)
    bg_key = max(clusters, key=lambda k: len(clusters[k]))
    bg = Counter(clusters[bg_key]).most_common(1)[0][0]
    ratios = [
        contrast_ratio(Counter(members).most_common(1)[0][0], bg)
        for key, members in clusters.items()
        if key != bg_key and len(members) >= STRONG_SHARE * len(pixels)
    ]
    return min(ratios) if ratios else None
