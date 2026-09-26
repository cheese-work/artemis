"""Anthropic prompt-cache breakpoints for the agent request path.

Anthropic caches on an exact prefix match and honours at most four
``cache_control`` breakpoints per request (render order: tools, then system,
then messages). Artemis re-sends the same tool definitions, the same static
system prompt and a byte-stable transcript prefix on every operator step, so
without a breakpoint the provider re-reads all of it at full price every turn.

Up to three breakpoints are placed, all on content that the ledger contract
makes byte-identical for the rest of the session:

1. **System.** The last message of the leading system run. A breakpoint there
   covers the bound tool definitions and the whole system prompt;
   :meth:`~artemis.memory.transcript.TranscriptLedger.set_static_prefix`
   refuses a second install, so that prefix never changes.
2. **Transcript (conservative).** The newest message older than the
   ``horizon_depths`` newest observations — see :func:`horizon_prefix_index`.
3. **Transcript (exact).** The newest message before the oldest one the scrub
   edge can still rewrite — see :func:`frozen_prefix_index`. This is normally
   far newer than (2) and is what carries the cache: Anthropic matches the
   longest cached prefix among the breakpoints present, so (2) costs nothing
   extra (the two write spans partition one range) and is the fallback that
   bounds the damage if (3) ever turned out to sit inside the mutable region.

A breakpoint inside the mutable tail would force a fresh cache *write* every
turn, which is worse than not caching at all, so both transcript anchors are
derived from evidence in the messages themselves and are simply omitted when
neither derivation applies.

Every other provider gets its own list back by identity: OpenAI and Google
already do automatic prefix caching and must keep today's request shape.
Opt out with ``ARTEMIS_ANTHROPIC_PROMPT_CACHE=0``.

Cache effectiveness is readable from the existing telemetry:
``input_token_details.cache_read`` becomes ``cached_tokens`` in
:func:`~artemis.llm.google.usage.normalize_usage` (hence
:meth:`~artemis.services.token_meter.SessionTokenMeter.cached_ratio`), and the
raw ``token_usage`` payload the trace DB stores keeps ``cache_creation``
alongside it, which is what tells a cache write apart from a cache read.
"""

import functools
import os
from typing import Any, Final

from langchain_core.messages import BaseMessage, SystemMessage

from artemis.memory.scrub_shape import has_pending_scrub_edits, is_screenshot_observation
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Environment opt-out. Caching is on by default for Anthropic: the failure
#: mode of a misplaced breakpoint is a wasted cache write, never a wrong
#: answer, and the provider ignores the directive when it cannot honour it.
ENV_FLAG: Final = "ARTEMIS_ANTHROPIC_PROMPT_CACHE"

_DISABLED_VALUES: Final = frozenset({"0", "false", "no", "off"})

CACHE_CONTROL: Final[dict[str, str]] = {"type": "ephemeral"}

#: Anthropic rejects a request carrying more than four breakpoints.
MAX_BREAKPOINTS: Final = 4

#: Block types a breakpoint may be attached to here. Images are deliberately
#: excluded: an observation deep enough to be an anchor has already had its
#: screenshot swapped for the visual summary text, so a text block is always
#: the natural carrier and no provider-specific image conversion is involved.
_CACHEABLE_BLOCK_TYPES: Final = frozenset({"text", "document", "tool_use", "tool_result"})

#: Fallback mutation horizon, in observation depths, when the agent config
#: cannot be read: ``memory.transcript.image_scrub_depth_relaxed`` (6) +
#: ``pending_grace_steps`` (3) from ``config/artemis.jsonc``. The text edge
#: (``xml_scrub_depth``, 1) is always shallower.
DEFAULT_HORIZON_DEPTHS: Final = 9


def prompt_cache_enabled() -> bool:
    """Whether Anthropic requests should carry cache breakpoints."""
    return os.environ.get(ENV_FLAG, "1").strip().lower() not in _DISABLED_VALUES


@functools.cache
def mutation_horizon_depths() -> int:
    """Observation depths within which the transcript may still be rewritten.

    The deepest scrub edge is the relaxed screenshot depth plus the pending
    grace window; the text edge is shallower by construction. Anything older
    than this is frozen — :class:`~artemis.agents.flash.context_compressor.ScrubEdgeCompressor`
    edits each message at most twice and never revisits it.

    Cached: the configuration is read once per process, on the first Anthropic
    request, not on every call.
    """
    cfg = _transcript_config()
    if cfg is None:
        return DEFAULT_HORIZON_DEPTHS
    image_depth = max(
        int(getattr(cfg, "image_scrub_depth", 3)),
        int(getattr(cfg, "image_scrub_depth_relaxed", 6)),
    )
    grace = int(getattr(cfg, "pending_grace_steps", 3))
    text_depth = int(getattr(cfg, "xml_scrub_depth", None) or 1)
    return max(1, image_depth + grace, text_depth)


def _transcript_config() -> Any | None:
    """The loaded ``memory.transcript`` config, or None when unreadable."""
    from artemis.config import load_agent_config

    try:
        return load_agent_config().memory.transcript
    except (OSError, ValueError) as e:
        logger.debug(f"Anthropic prompt cache: falling back to default horizon ({e}).")
        return None


def leading_system_index(messages: list[BaseMessage]) -> int | None:
    """Index of the last message in the leading system run, if any."""
    last = None
    for idx, message in enumerate(messages):
        if not isinstance(message, SystemMessage):
            break
        last = idx
    return last


def frozen_prefix_index(messages: list[BaseMessage]) -> int | None:
    """Index of the newest message before the oldest one still open to a rewrite.

    :func:`~artemis.agents.flash.context_compressor.has_pending_scrub_edits`
    answers, from the message itself, whether the scrub edge can still rewrite
    it — each of the edge's three content edits is gated on something the
    message still carries. The oldest message answering it is therefore the
    exact frontier of the mutable region, and everything before it is frozen
    for the rest of the session: the anchor is placed one message earlier.

    This is the anchor that makes the cache *grow*. The frontier only ever
    moves forward — a resolved message is never revisited and new observations
    arrive at the tail — so the cached prefix extends by roughly one turn per
    turn instead of plateauing.

    Returns None when no message is rewritable at all. A message list the scrub
    edge never touched (any non-ledger caller) carries no evidence of where its
    mutable region starts, and guessing there is exactly the failure this
    module must not risk.
    """
    for index, message in enumerate(messages):
        if has_pending_scrub_edits(message):
            return index - 1 if index > 0 else None
    return None


def horizon_prefix_index(messages: list[BaseMessage], horizon_depths: int) -> int | None:
    """Index of the newest message older than the ``horizon_depths`` newest observations.

    Walks back from the tail counting screenshot-edge observations — the same
    depth ladder :meth:`~artemis.agents.flash.context_compressor.ScrubEdgeCompressor._scrub_edge`
    counts, recognised through
    :func:`~artemis.agents.flash.context_compressor.is_screenshot_observation`
    whether the image is still there or has already been resolved in place —
    and stops one message before the ``horizon_depths``-th of them. Every
    message the edge may still rewrite lies at a depth no greater than the
    horizon, so everything before that message is frozen.

    Returns None for a conversation with fewer than ``horizon_depths``
    observations: those requests have no proven stable transcript prefix yet.
    """
    depths = max(1, horizon_depths)
    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        if not is_screenshot_observation(messages[index]):
            continue
        seen += 1
        if seen >= depths:
            return index - 1 if index > 0 else None
    return None


def transcript_anchors(messages: list[BaseMessage], horizon_depths: int) -> list[int]:
    """The transcript breakpoint indexes, oldest first (possibly empty).

    Both derivations must apply before either is used: they are independent
    arguments for the same conclusion, and a disagreement about whether this
    list even is a scrubbed transcript is a reason to place nothing.
    """
    frozen = frozen_prefix_index(messages)
    aged = horizon_prefix_index(messages, horizon_depths)
    if frozen is None or aged is None:
        return []
    return [aged, frozen] if aged < frozen else [frozen]


def stable_prefix_index(messages: list[BaseMessage], horizon_depths: int) -> int | None:
    """The oldest (most conservative) transcript anchor, or None if there is none."""
    anchors = transcript_anchors(messages, horizon_depths)
    return anchors[0] if anchors else None


def _has_cache_control(messages: list[BaseMessage]) -> bool:
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and "cache_control" in b for b in content):
            return True
    return False


def _marked(message: BaseMessage) -> BaseMessage | None:
    """A copy of ``message`` carrying a breakpoint, or None if it cannot hold one.

    The caller's message object is never touched: the content list and the one
    block that gains ``cache_control`` are rebuilt, everything else is shared
    by reference.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        if not content.strip():
            return None
        return message.model_copy(
            update={"content": [{"type": "text", "text": content, "cache_control": CACHE_CONTROL}]}
        )
    if not isinstance(content, list):
        return None
    for position in range(len(content) - 1, -1, -1):
        block = content[position]
        if isinstance(block, str):
            if not block.strip():
                continue
            replacement: dict[str, Any] = {
                "type": "text",
                "text": block,
                "cache_control": CACHE_CONTROL,
            }
        elif isinstance(block, dict) and block.get("type") in _CACHEABLE_BLOCK_TYPES:
            replacement = {**block, "cache_control": CACHE_CONTROL}
        else:
            continue
        blocks = list(content)
        blocks[position] = replacement
        return message.model_copy(update={"content": blocks})
    return None


def apply_cache_breakpoints(
    messages: list[BaseMessage], *, horizon_depths: int | None = None
) -> list[BaseMessage]:
    """Return ``messages`` with Anthropic cache breakpoints on its stable prefix.

    Returns the caller's own list, unchanged and by identity, whenever nothing
    is placed — disabled by flag, breakpoints already present (the operation is
    idempotent), or no anchor that can carry one.
    """
    if not messages or not prompt_cache_enabled() or _has_cache_control(messages):
        return messages

    depths = mutation_horizon_depths() if horizon_depths is None else horizon_depths
    system_index = leading_system_index(messages)
    anchors = [] if system_index is None else [system_index]
    anchors += [
        index
        for index in transcript_anchors(messages, depths)
        if system_index is None or index > system_index
    ]

    marked = list(messages)
    applied = 0
    for index in anchors[:MAX_BREAKPOINTS]:
        message = _marked(marked[index])
        if message is not None:
            marked[index] = message
            applied += 1
    if not applied:
        return messages
    return marked
