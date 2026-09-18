"""Anthropic prompt-cache breakpoints for the agent request path.

Anthropic caches on an exact prefix match and honours at most four
``cache_control`` breakpoints per request (render order: tools, then system,
then messages). Artemis re-sends the same tool definitions, the same static
system prompt and a byte-stable transcript prefix on every operator step, so
without a breakpoint the provider re-reads all of it at full price every turn.

Two breakpoints are placed, both on content that the ledger contract makes
byte-identical for the rest of the session:

1. **System.** The last message of the leading system run. A breakpoint there
   covers the bound tool definitions and the whole system prompt;
   :meth:`~artemis.memory.transcript.TranscriptLedger.set_static_prefix`
   refuses a second install, so that prefix never changes.
2. **Transcript.** The newest message that is provably behind the ledger's
   mutation horizon — see :func:`stable_prefix_index`. A breakpoint inside the
   mutable tail would force a fresh cache *write* every turn, which is worse
   than not caching at all, so the anchor is derived conservatively and simply
   omitted when it cannot be proven.

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

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

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


def stable_prefix_index(messages: list[BaseMessage], horizon_depths: int) -> int | None:
    """Index of the newest message provably older than the mutation horizon.

    The horizon is counted in *observation depths*, not messages, so the index
    is derived from the ledger's message shape rather than a raw offset: every
    committed turn contributes exactly one observation ``HumanMessage``
    (:meth:`~artemis.memory.transcript.TranscriptLedger.commit_staged` keys the
    turn's step to its first image-bearing message) and at most one result
    ``HumanMessage``; every other message in a turn is an ``AIMessage`` or a
    ``ToolMessage``. At least half of the trailing human messages are therefore
    observations, so leaving ``2 * horizon + 1`` human messages after the anchor
    puts at least ``horizon`` observations behind it — the anchor sits at
    observation depth > horizon, past the deepest scrub edge.

    Returns None for a conversation too short to prove that, which is the
    correct answer: those requests have no stable transcript prefix worth a
    breakpoint yet.
    """
    needed = 2 * max(1, horizon_depths) + 1
    human_indexes = [idx for idx, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_indexes) < needed:
        return None
    return human_indexes[-needed]


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
    transcript_index = stable_prefix_index(messages, depths)
    if transcript_index is not None and (system_index is None or transcript_index > system_index):
        anchors.append(transcript_index)

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
