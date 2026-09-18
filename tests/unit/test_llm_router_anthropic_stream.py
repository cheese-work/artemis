# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression test for the Anthropic streaming `model_dump` crash (CHE-643).

``langchain_anthropic``'s ``ChatAnthropic._make_message_chunk_from_anthropic_event``
reads ``event.context_management`` off `message_delta` stream events and calls
``.model_dump()`` on it unconditionally. The Anthropic SDK's `RawMessageDeltaEvent`
has no typed field for `context_management` (that name is only declared on
`ChatAnthropic` itself, as a plain request-side dict), so when the API includes
that block in a response, Pydantic's `extra="allow"` stores it as a raw `dict`
instead of a submodel — and `.model_dump()` raises
`AttributeError: 'dict' object has no attribute 'model_dump'`, discarding the
whole in-flight response (see `artemis/services/llm.py::_complete_attempt`).
"""

import inspect
from unittest.mock import patch

from anthropic.types.raw_message_delta_event import Delta, RawMessageDeltaEvent
from anthropic.types.usage import Usage
import pytest

from artemis.llm.router import _patch_anthropic_stream_context_management


def _unpatched_make_message_chunk():
    """The original langchain-anthropic method, regardless of test order.

    Other tests/modules construct real Anthropic models (e.g.
    ``test_llm_grounding.py``), which applies this module's process-global
    patch as a side effect. Walk the `__wrapped__`-style closure our patch
    builds to recover the pristine method so this test proves the upstream
    bug independent of suite ordering.
    """
    from langchain_anthropic import ChatAnthropic

    current = ChatAnthropic._make_message_chunk_from_anthropic_event
    closure = inspect.getclosurevars(current).nonlocals
    return closure.get("original", current)


def _delta_event_with_raw_context_management() -> RawMessageDeltaEvent:
    """Builds the exact malformed event shape the live Anthropic API sent."""
    delta = Delta.model_construct(
        stop_reason="end_turn",
        stop_sequence=None,
        container=None,
    )
    return RawMessageDeltaEvent.model_construct(
        type="message_delta",
        delta=delta,
        usage=Usage(input_tokens=1, output_tokens=1),
        context_management={"applied_edits": []},
    )


def test_raw_dict_context_management_crashes_without_the_patch():
    """Reproduces the upstream bug so the workaround has something to guard against."""
    from langchain_anthropic import ChatAnthropic

    model = ChatAnthropic.model_construct(model="claude-sonnet-5")
    event = _delta_event_with_raw_context_management()
    unpatched = _unpatched_make_message_chunk()

    with patch.object(ChatAnthropic, "_make_message_chunk_from_anthropic_event", unpatched):
        with pytest.raises(AttributeError, match="model_dump"):
            model._make_message_chunk_from_anthropic_event(
                event, stream_usage=True, coerce_content_to_string=True
            )


def test_patch_normalizes_raw_dict_context_management():
    from langchain_anthropic import ChatAnthropic

    _patch_anthropic_stream_context_management()

    model = ChatAnthropic.model_construct(model="claude-sonnet-5")
    event = _delta_event_with_raw_context_management()

    chunk, _ = model._make_message_chunk_from_anthropic_event(
        event, stream_usage=True, coerce_content_to_string=True
    )

    assert chunk is not None
    assert chunk.response_metadata["context_management"] == {"applied_edits": []}


def test_patch_normalizes_raw_dict_container():
    """The same unconditional `.model_dump()` pattern applies to `delta.container`."""
    from langchain_anthropic import ChatAnthropic

    _patch_anthropic_stream_context_management()

    model = ChatAnthropic.model_construct(model="claude-sonnet-5")
    delta = Delta.model_construct(stop_reason="end_turn", stop_sequence=None)
    delta.container = {"id": "container_123"}
    event = RawMessageDeltaEvent.model_construct(
        type="message_delta",
        delta=delta,
        usage=Usage(input_tokens=1, output_tokens=1),
    )

    chunk, _ = model._make_message_chunk_from_anthropic_event(
        event, stream_usage=True, coerce_content_to_string=True
    )

    assert chunk is not None
    assert chunk.response_metadata["container"] == {"id": "container_123"}


def test_patch_is_idempotent_across_multiple_model_constructions():
    from langchain_anthropic import ChatAnthropic

    _patch_anthropic_stream_context_management()
    patched_once = ChatAnthropic._make_message_chunk_from_anthropic_event

    # Re-applying (as every Anthropic model construction does) must not wrap
    # the wrapper again — otherwise each new stream event pays for one more
    # layer of indirection for the lifetime of the process.
    _patch_anthropic_stream_context_management()
    _patch_anthropic_stream_context_management()

    assert ChatAnthropic._make_message_chunk_from_anthropic_event is patched_once
