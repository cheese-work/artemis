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

"""List prices for the estimated API cost shown beside a session's tokens.

The table is static and public: copied by hand from the providers' own
pricing pages (no paid price service, no network call at runtime). Refresh it
when a provider changes a price or a model is added; ``PRICES_AS_OF`` says
when it was last checked.

Sources, read on 2026-10-16:
- OpenAI: https://developers.openai.com/api/docs/pricing (standard tier)
- Google: https://ai.google.dev/gemini-api/docs/pricing (paid standard tier)
- Anthropic: https://platform.claude.com/docs/en/about-claude/pricing

Rates are the short-context base rates. A provider's long-context or
time-limited rate (OpenAI above 272k input, Gemini 3.x Flash from 2027-01-01)
is not applied, so this is an estimate at list price and not a bill.
"""

PRICES_AS_OF = "2026-10-16"

# model id -> (input, cached input, output), USD per 1M tokens.
_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    # OpenAI
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    # Google
    "gemini-4-argon": (2.00, 0.10, 10.00),
    "gemini-3.8-flash": (0.75, 0.075, 3.75),
    "gemini-3.6-flash": (0.75, 0.075, 3.75),
    "gemini-3.5-flash-lite": (0.30, 0.03, 2.50),
    "gemini-3.1-flash-lite": (0.25, 0.025, 1.50),
    "gemini-3.1-pro-preview": (2.00, 0.20, 12.00),
    "gemini-3-flash-preview": (0.50, 0.05, 3.00),
    "gemini-2.5-pro": (1.25, 0.125, 10.00),
    "gemini-2.5-flash": (0.30, 0.03, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.01, 0.40),
    # Anthropic
    "claude-fable-5.1": (10.00, 0.25, 50.00),
    "claude-fable-5": (10.00, 1.00, 50.00),
    "claude-opus-5.5": (4.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-opus-4.8": (5.00, 0.50, 25.00),
    "claude-opus-4.7": (5.00, 0.50, 25.00),
    "claude-sonnet-5.5": (2.00, 0.10, 10.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-haiku-5.5": (0.10, 0.01, 0.50),
    "claude-haiku-4.5": (1.00, 0.10, 5.00),
}


def _key(model: str) -> str:
    """Lowercase id without a ``provider/`` prefix, with ``-`` and ``.`` equal."""
    return model.strip().lower().rsplit("/", 1)[-1].replace(".", "-")


_PRICES = {_key(name): rates for name, rates in _PRICES_PER_MTOK.items()}


def normalize_model(model: object) -> str | None:
    """The model id as the price table names it (no ``provider/`` prefix); None if blank."""
    name = str(model or "").strip().rsplit("/", 1)[-1]
    return name or None


def estimate_cost_usd(
    model: object, prompt_tokens: int, cached_tokens: int, completion_tokens: int
) -> float | None:
    """Estimated cost at list price, or None when the model has no listed price.

    ``prompt_tokens`` includes the cached share, which is billed at the
    cached-input rate instead of the input rate.
    """
    name = normalize_model(model)
    rates = _PRICES.get(_key(name)) if name else None
    if rates is None:
        return None
    input_rate, cached_rate, output_rate = rates
    cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
    uncached = max(prompt_tokens, 0) - cached
    cost = uncached * input_rate + cached * cached_rate + max(completion_tokens, 0) * output_rate
    return round(cost / 1_000_000, 6)
