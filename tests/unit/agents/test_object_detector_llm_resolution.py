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

"""CHE-646: object_detector must resolve its own configured model from the
llm utils config, and must never silently fall back to the operator model
without a visible log signal.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

from artemis.agents.object_detector import object_detector as od
from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback
import pytest


def _empty_response_llm() -> MagicMock:
    """A fake LLM whose ainvoke resolves to an empty-list JSON response, so
    _detect_single_label completes cleanly without hitting the network."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content="[]"))
    return llm


_REQUIRED_NODES = (
    "planner",
    "summarizer",
    "operator",
    "operator_summarizer",
    "log_reader_sub_agent",
    "log_analyzer",
    "diagnoser",
    "checker",
    "planner_avatar",
    "history_analyzer_expert",
    "diagnoser_expert",
    "explorer",
)


def _llm(provider: str = "openai", model: str = "gpt-5.6-sol") -> LLMWithFallback:
    return LLMWithFallback(
        provider=provider, model=model, fallback=LLM(provider=provider, model=model)
    )


def _config(object_detector: LLMWithFallback | None) -> LLMConfig:
    """A fully-populated LLMConfig; utils.object_detector varies per test."""
    node = _llm()
    return LLMConfig(
        **{n: node for n in _REQUIRED_NODES},
        utils=LLMConfigUtils(
            outputter=node,
            hopper=node,
            object_detector=object_detector,
        ),
    )


@pytest.mark.asyncio
async def test_configured_object_detector_resolves_via_utils_not_operator(monkeypatch):
    """When utils.object_detector IS configured, the detector must resolve to
    that configured entry via get_llm(name='object_detector', is_utils=True),
    never to the operator entry."""
    detector_llm = _llm(model="detector-model")
    ctx = MagicMock()
    ctx.llm_config = _config(object_detector=detector_llm)

    calls = []

    def fake_get_llm(ctx_arg, name, is_utils=False, **kwargs):
        calls.append({"name": name, "is_utils": is_utils})
        return _empty_response_llm()

    monkeypatch.setattr(od, "get_llm", fake_get_llm)

    result = await od._run_object_detection(
        ctx, image_bytes=b"fake-image-bytes", queries=["button"]
    )

    assert result["detected"] == []
    # Exactly one get_llm call, for the configured detector node -- never operator.
    assert calls == [{"name": "object_detector", "is_utils": True}]


@pytest.mark.asyncio
async def test_unconfigured_object_detector_logs_warning_and_falls_back_to_operator(
    monkeypatch, caplog
):
    """When utils.object_detector is None, get_llm(is_utils=True) raises ValueError
    (see LLMConfig.get_utils). The detector must log a visible warning AND fall
    back to the operator model -- the fallback must remain functional, but the
    condition must no longer be silently swallowed."""
    ctx = MagicMock()
    ctx.llm_config = _config(object_detector=None)

    calls = []

    def fake_get_llm(ctx_arg, name, is_utils=False, **kwargs):
        calls.append({"name": name, "is_utils": is_utils})
        if name == "object_detector":
            raise ValueError("Utils 'object_detector' is not configured.")
        return _empty_response_llm()

    monkeypatch.setattr(od, "get_llm", fake_get_llm)

    with caplog.at_level(logging.WARNING, logger="artemis.agents.object_detector.object_detector"):
        result = await od._run_object_detection(
            ctx, image_bytes=b"fake-image-bytes", queries=["button"]
        )

    assert result["detected"] == []
    # Both calls happened: the failed primary attempt, then the operator fallback.
    assert calls == [
        {"name": "object_detector", "is_utils": True},
        {"name": "operator", "is_utils": False},
    ]
    # The fallback condition must be visible in logs, not silently swallowed.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "object_detector" in r.getMessage() and "operator" in r.getMessage() for r in warnings
    )


def test_prefix_path_without_is_utils_raises_attribute_error():
    """Regression guard: object_detector is a field of LLMConfigUtils, not of
    LLMConfig. get_agent('object_detector') (the pre-fix, is_utils=False path)
    must still raise AttributeError -- proving the production call site must
    pass is_utils=True and cannot silently rely on get_agent's defaulting."""
    config = _config(object_detector=_llm())

    with pytest.raises(AttributeError):
        config.get_agent("object_detector")  # type: ignore[arg-type]

    # The correct (is_utils=True) path resolves cleanly instead.
    resolved = config.get_utils("object_detector")
    assert resolved.model == "gpt-5.6-sol"
