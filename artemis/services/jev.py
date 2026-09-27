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

"""TypeSafe AI "System One" (Jev) client: typed decisions over text state.

Jev answers structured questions about structured state and returns typed
values with calibrated confidence -- no free text, so it cannot be asked to
explain itself and cannot hallucinate a field outside the declared schema.
It is used here for hot-path decisions where a frontier LLM's latency is
unaffordable but hand-tuned weights are too brittle.

Three question primitives are supported, matching the vendor API:

``choice``
    Pick one label from ``criteria``. Answers carry ``choice``,
    ``probabilities`` and ``confidence``.
``score``
    Rate the state against an ordered rubric. Answers carry ``score``,
    ``legend`` and ``confidence``.
``noul``
    Judge a statement's truth. Answers carry ``noul`` in ``[0, 1]``.

Questions in one request are evaluated in parallel and in isolation against
the same state, so asking several narrow ones costs about the same as asking
one and avoids the context bleed of a single compound question. The vendor's
guidance is to keep each question scoped to one thing and combine the answers
in your own deterministic code -- decompose rather than widen.

**Every caller must treat this service as optional.** It is reached over the
network, it is gated on a key we may not hold, and the vendor is early-stage:
:func:`ask` returns ``None`` for *every* failure mode (disabled, unconfigured,
transport error, malformed payload) rather than raising, so call sites degrade
to their existing logic instead of breaking a device run. Callers that need to
distinguish "Jev said nothing" from "Jev said no" must check for ``None``
before reading the answer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from typing import Any

import httpx

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"

#: Hot-path budget. The vendor advertises 70-500ms; anything past this and the
#: heuristic fallback is cheaper than continuing to wait, because the caller is
#: holding up a device action.
DEFAULT_TIMEOUT_SECONDS = 2.0

#: Vendor-documented ceiling on ``choice`` labels.
MAX_CHOICE_CARDINALITY = 255


class JevError(Exception):
    """Raised inside the client and converted to ``None`` at the boundary."""


@dataclass(frozen=True)
class ChoiceAnswer:
    """One ``choice`` verdict plus its confidence.

    ``confidence`` is calibrated across groups of predictions, not per
    answer: the vendor states it "does not guarantee that an individual
    answer is correct". Threshold it to improve the rate of good outcomes;
    never read a single high value as proof this particular answer is right.
    """

    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class NoulAnswer:
    """One ``noul`` verdict: the probability the statement is true."""

    noul: float


def choice_question(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    """Builds a ``choice`` question body.

    ``criteria`` maps each label to a description of when it applies; the
    descriptions are the only guidance Jev gets, so they carry the semantics
    that a prompt would otherwise carry.
    """
    if not criteria:
        raise ValueError("choice question requires at least one criterion")
    if len(criteria) > MAX_CHOICE_CARDINALITY:
        raise ValueError(
            f"choice cardinality {len(criteria)} exceeds the supported maximum"
            f" of {MAX_CHOICE_CARDINALITY}"
        )
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def noul_question(instructions: str) -> dict[str, Any]:
    """Builds a ``noul`` (is-this-true) question body."""
    return {"type": "noul", "instructions": instructions}


def _finite_float(value: Any) -> float | None:
    """Coerces a JSON number to a finite float, or ``None`` if it is not one.

    ``isinstance(value, (int, float))`` is not enough on its own: JSON has no
    integer width limit, so a large enough literal passes the type check and
    then raises ``OverflowError`` in ``float()``. NaN and the infinities parse
    cleanly but would poison every downstream comparison. Both are malformed
    input, and this module drops malformed input rather than raising.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _parse_choice(raw: Any) -> ChoiceAnswer | None:
    """Parses one ``choice`` answer, rejecting anything malformed.

    Schema conformance is the vendor's headline guarantee, but it is a claim
    about their model, not about the network between us -- a proxy error page
    or a truncated body reaches us through the same channel. Validate rather
    than trust.
    """
    if not isinstance(raw, dict):
        return None
    choice = raw.get("choice")
    if not isinstance(choice, str) or not choice:
        return None

    probabilities_raw = raw.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probabilities_raw, dict):
        probabilities = {
            str(label): number
            for label, p in probabilities_raw.items()
            if (number := _finite_float(p)) is not None
        }

    confidence = _finite_float(raw.get("confidence"))
    if confidence is None:
        # Confidence is what the call sites gate on. Absent it, the answer is
        # unusable -- treat as no answer rather than inventing a default.
        return None

    return ChoiceAnswer(
        choice=choice,
        probabilities=probabilities,
        confidence=confidence,
    )


def _parse_noul(raw: Any) -> NoulAnswer | None:
    """Parses one ``noul`` answer into a probability in ``[0, 1]``."""
    if not isinstance(raw, dict):
        return None
    value = _finite_float(raw.get("noul"))
    if value is None:
        return None
    return NoulAnswer(noul=value)


def parse_answers(payload: Any) -> dict[str, ChoiceAnswer | NoulAnswer]:
    """Extracts every well-formed answer from a response body.

    Malformed individual answers are dropped rather than failing the whole
    response: a caller asking two questions can still act on the one that
    came back intact.
    """
    if not isinstance(payload, dict):
        return {}
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return {}

    parsed: dict[str, ChoiceAnswer | NoulAnswer] = {}
    for name, raw in answers.items():
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        answer: ChoiceAnswer | NoulAnswer | None
        if kind == "choice":
            answer = _parse_choice(raw)
        elif kind == "noul":
            answer = _parse_noul(raw)
        else:
            # score, or a primitive the vendor adds later: not consumed here.
            continue
        if answer is not None:
            parsed[str(name)] = answer
    return parsed


class JevClient:
    """Async client for the System One endpoint.

    The client is intentionally thin: one POST, a strict timeout, and a parse
    that refuses to guess. Retries are deliberately absent -- callers are on a
    device hot path and their fallback is cheaper than a second round trip.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._client = client

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/systemone"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._client is not None:
            response = await self._client.post(
                url, json=body, headers=headers, timeout=self._timeout
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()

    async def ask(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, ChoiceAnswer | NoulAnswer]:
        """Asks one or more questions about ``state``. Raises on failure."""
        if not questions:
            return {}
        body = {"state": state, "model": self._model, "questions": questions}
        try:
            payload = await self._post(body)
        except httpx.HTTPStatusError as e:
            raise JevError(f"Jev returned HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise JevError(f"Jev transport error: {e!r}") from e
        except ValueError as e:  # non-JSON body
            raise JevError(f"Jev returned a non-JSON body: {e!r}") from e
        return parse_answers(payload)


def build_client(settings: Any) -> JevClient | None:
    """Builds a client from settings, or ``None`` when Jev is not usable.

    Returns ``None`` -- never raises -- when the feature flag is off or no key
    is configured, which is the expected state for every run that has not
    opted in.
    """
    if not getattr(settings, "ARTEMIS_JEV_ENABLED", False):
        return None

    raw_key = getattr(settings, "TYPESAFE_API_KEY", None)
    if raw_key is None:
        logger.debug("Jev is enabled but TYPESAFE_API_KEY is unset; staying disabled.")
        return None
    # Settings stores the key as a SecretStr; unwrap only at the call boundary.
    api_key = raw_key.get_secret_value() if hasattr(raw_key, "get_secret_value") else str(raw_key)
    if not api_key:
        logger.debug("Jev is enabled but TYPESAFE_API_KEY is empty; staying disabled.")
        return None

    base_url = getattr(settings, "TYPESAFE_BASE_URL", None) or DEFAULT_BASE_URL
    model = getattr(settings, "ARTEMIS_JEV_MODEL", None) or DEFAULT_MODEL
    timeout = getattr(settings, "ARTEMIS_JEV_TIMEOUT_SECONDS", None) or DEFAULT_TIMEOUT_SECONDS
    return JevClient(api_key, base_url=base_url, model=model, timeout=float(timeout))


async def ask(
    client: JevClient | None,
    state: str,
    questions: dict[str, dict[str, Any]],
) -> dict[str, ChoiceAnswer | NoulAnswer] | None:
    """Best-effort ask: ``None`` on any Jev failure, so callers can fall back.

    This is the function hot paths should use. Every way this service can
    fail -- transport, HTTP status, an unparseable body -- reaches here as
    ``JevError`` and becomes ``None``; the parser drops malformed answers
    rather than raising, so no other failure mode is expected. It does not
    blanket-catch ``Exception``: a bug in this module, or a genuinely
    unexpected error, should surface rather than be silently downgraded to a
    fallback. ``CancelledError`` propagates so cancellation still works.
    """
    if client is None:
        return None
    try:
        return await client.ask(state, questions)
    except asyncio.CancelledError:
        raise
    except JevError as e:
        logger.warning(f"Jev call failed ({e}); falling back to local logic.")
        return None
