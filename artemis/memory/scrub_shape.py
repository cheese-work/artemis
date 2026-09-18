"""The shape of a transcript message, as the scrub edge sees it.

:class:`~artemis.agents.flash.context_compressor.ScrubEdgeCompressor` decides
what to rewrite, and at what depth, from a handful of tests on a message's
content blocks. Those tests are the single definition of "this is an
observation" and "this message can still change", and more than the compressor
needs to agree on them: the Anthropic prompt cache
(:mod:`artemis.llm.anthropic_cache`) places its breakpoints on the strength of
exactly the same reading, and a breakpoint that lands one message inside the
mutable region turns every cache read into a cache write. They live here, in
one place, so the compressor and its readers cannot drift apart.

The markers and artefacts are the compressor's own output, so a reader who only
ever sees the rendered message list — which is all a request carries — can tell
an observation the screenshot edge has already resolved from one it has not yet
reached, without knowing anything about ledger bookkeeping.
"""

from typing import Any, Final

from langchain_core.messages import BaseMessage

from artemis.memory.transcript import (
    EPHEMERAL_BLOCKS_KEY,
    PLAN_RECITATION_MARKER,
    PRO_UI_LIST_MARKER,
)

#: Header written above a resolved visual summary (public: the chunk capsule
#: lens checks it to avoid repeating a summary already present verbatim, and it
#: identifies an observation whose screenshot has been resolved in place).
HISTORY_SUMMARY_PREFIX: Final = "--- Historical Visual Transition ---\n"

#: Text prefixes of the two placeholders the screenshot edge writes when no
#: summary can be had — a failed summary job, or one still pending after the
#: grace window closed.
SUMMARY_UNAVAILABLE_PREFIX: Final = "[visual summary unavailable;"
SUMMARY_PENDING_PREFIX: Final = "[visual summary pending;"

_RESOLVED_IMAGE_PREFIXES: Final = (
    HISTORY_SUMMARY_PREFIX,
    SUMMARY_UNAVAILABLE_PREFIX,
    SUMMARY_PENDING_PREFIX,
)

#: Text of the label block both observation shapes place directly above the
#: screenshot. It only makes sense next to an image, so it leaves together with
#: the image it labels (the visual summary carries its own header).
SCREENSHOT_LABEL: Final = "--- Current Screenshot ---"

#: The Flash runner's legacy heavy-block marker, and the compressor's default.
LEGACY_UI_LIST_MARKER: Final = "--- UI Element List ---"

#: Every strip marker any profile installs: the legacy default plus the two the
#: Pro transcript ledger passes (``TranscriptLedger.__init__``). A reader that
#: cannot know which profile produced a message list tests all of them, which
#: only ever over-reports a message as still editable.
ALL_STRIP_MARKERS: Final = (LEGACY_UI_LIST_MARKER, PRO_UI_LIST_MARKER, PLAN_RECITATION_MARKER)


def content_blocks(message: BaseMessage) -> list[Any]:
    """``message``'s content blocks, or an empty list for string content.

    String content is never rewritten: the compressor's edits all address
    blocks by index and it returns early on anything that is not a list.
    """
    content = getattr(message, "content", None)
    return content if isinstance(content, list) else []


def text_has_strip_marker(text: str, markers: tuple[str, ...] = ALL_STRIP_MARKERS) -> bool:
    """Whether ``text`` carries a heavy block the text edge cuts at its marker."""
    return any(marker in text for marker in markers)


def has_image_block(message: BaseMessage) -> bool:
    """Whether ``message`` still carries a screenshot.

    The screenshot edge's own discovery test: a message answering it is on the
    compressor's depth ladder and its image has not been resolved yet.
    """
    return any(
        isinstance(block, dict) and block.get("type") in ("image_url", "image")
        for block in content_blocks(message)
    )


def has_strip_marker(message: BaseMessage, markers: tuple[str, ...] = ALL_STRIP_MARKERS) -> bool:
    """Whether ``message`` still carries a heavy block the text edge cuts."""
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and text_has_strip_marker(str(block.get("text", "")), markers)
        for block in content_blocks(message)
    )


def ephemeral_block_indices(message: BaseMessage) -> set[int]:
    """Indices of ``message``'s content blocks still marked ephemeral.

    Empty once the text edge has deleted them — the key is dropped as it is
    consumed — so a non-empty result means the edge still owes this message a
    deletion.
    """
    kwargs = getattr(message, "additional_kwargs", None)
    if not isinstance(kwargs, dict):
        return set()
    indices: set[int] = set()
    for value in kwargs.get(EPHEMERAL_BLOCKS_KEY) or []:
        try:
            indices.add(int(value))
        except (TypeError, ValueError):
            continue
    return indices


def has_pending_scrub_edits(
    message: BaseMessage, markers: tuple[str, ...] = ALL_STRIP_MARKERS
) -> bool:
    """Whether the scrub edge can still rewrite ``message``'s content blocks.

    ``ScrubEdgeCompressor._rewrite`` makes exactly three content edits, and each
    is gated on something the message still carries: the screenshot swap (and
    the removal of the :data:`SCREENSHOT_LABEL` block above it) on an image
    block, the heavy-block strip on a strip marker, and the per-turn deletion on
    surviving :data:`~artemis.memory.transcript.EPHEMERAL_BLOCKS_KEY` indices. A
    message answering none of the three has had all three done to it — or never
    qualified for any — and is frozen: its content is byte-stable for the rest
    of the session.
    """
    return (
        has_image_block(message)
        or has_strip_marker(message, markers)
        or bool(ephemeral_block_indices(message))
    )


def is_screenshot_observation(message: BaseMessage) -> bool:
    """Whether ``message`` occupies one of the screenshot edge's depths.

    True for an observation whose image is still there and for one the edge has
    already resolved, which it leaves carrying its own artefact (the historical
    visual-transition header or one of the two placeholders). An image dropped
    because no summary job existed at all leaves no artefact and so reads as a
    non-observation; that only ever makes a depth count too *small*, and an
    anchor derived from such a count correspondingly older.
    """
    if has_image_block(message):
        return True
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and str(block.get("text", "")).startswith(_RESOLVED_IMAGE_PREFIXES)
        for block in content_blocks(message)
    )
