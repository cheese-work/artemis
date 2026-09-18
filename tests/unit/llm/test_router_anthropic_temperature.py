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

"""CHE-640: the Anthropic branch must omit `temperature` for models that
reject it outright (HTTP 400 `temperature is deprecated for this model`),
while still sending it for models that accept it.

No network call is made anywhere in this module -- `ChatAnthropic()`
construction only builds the client object; the HTTP request happens on
`.invoke()`, which these tests never call.
"""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic

from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


def _anthropic_endpoint(model_name: str, **overrides) -> ModelEndpoint:
    return ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name=model_name,
        api_key="test-key-not-real",
        **overrides,
    )


def _create_anthropic(model_name: str, **overrides) -> ChatAnthropic:
    model = ModelFactory.create_model(_anthropic_endpoint(model_name, **overrides))
    assert isinstance(model, ChatAnthropic)
    return model


class TestAnthropicTemperatureOmission:
    def test_claude_sonnet_5_gets_no_temperature_kwarg(self):
        model = _create_anthropic("claude-sonnet-5")
        assert model.temperature is None

    def test_claude_opus_5_gets_no_temperature_kwarg(self):
        model = _create_anthropic("claude-opus-5")
        assert model.temperature is None

    def test_claude_haiku_4_5_gets_no_temperature_kwarg(self):
        model = _create_anthropic("claude-haiku-4-5-20251001")
        assert model.temperature is None

    def test_accepting_model_still_receives_configured_temperature(self):
        # claude-sonnet-4-5-20250929 is not in the rejecting-prefix list --
        # existing behavior for every model that still accepts the
        # parameter must be unchanged.
        model = _create_anthropic("claude-sonnet-4-5-20250929", temperature=0.0)
        assert model.temperature == 0.0

    def test_thinking_budget_does_not_reintroduce_temperature_for_rejecting_model(self):
        # The thinking-budget branch normally forces temperature=1.0; for a
        # model that rejects the parameter outright, forcing it would also
        # 400, so it must stay omitted even with a thinking budget set.
        model = _create_anthropic("claude-sonnet-5", thinking_budget=8192)
        assert model.temperature is None
        assert model.thinking is not None

    def test_thinking_budget_still_forces_temperature_one_for_accepting_model(self):
        model = _create_anthropic("claude-sonnet-4-5-20250929", thinking_budget=8192)
        assert model.temperature == 1.0
