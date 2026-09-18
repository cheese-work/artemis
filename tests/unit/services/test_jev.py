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

"""Tests for the Jev (TypeSafe System One) client.

The contract under test is mostly negative: every failure mode must degrade to
``None`` so the callers' fallbacks run, and no failure may escape as an
exception onto a device hot path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from artemis.services import jev


def _settings(**overrides):
    base = {
        "ARTEMIS_JEV_ENABLED": True,
        "TYPESAFE_API_KEY": "sk-test",
        "TYPESAFE_BASE_URL": None,
        "ARTEMIS_JEV_MODEL": None,
        "ARTEMIS_JEV_TIMEOUT_SECONDS": 2.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- question builders ---------------------------------------------------


def test_choice_question_rejects_empty_criteria():
    with pytest.raises(ValueError):
        jev.choice_question("pick one", {})


def test_choice_question_rejects_cardinality_over_limit():
    criteria = {f"label_{i}": "desc" for i in range(jev.MAX_CHOICE_CARDINALITY + 1)}
    with pytest.raises(ValueError):
        jev.choice_question("pick one", criteria)


def test_choice_question_accepts_cardinality_at_limit():
    criteria = {f"label_{i}": "desc" for i in range(jev.MAX_CHOICE_CARDINALITY)}
    question = jev.choice_question("pick one", criteria)
    assert question["type"] == "choice"


# --- answer parsing ------------------------------------------------------


def test_parse_answers_reads_choice_and_noul():
    payload = {
        "answers": {
            "status": {
                "type": "choice",
                "choice": "present",
                "probabilities": {"present": 0.84, "shifted": 0.16},
                "confidence": 0.72,
            },
            "urgent": {"type": "noul", "noul": 0.99},
        }
    }
    answers = jev.parse_answers(payload)
    status = answers["status"]
    urgent = answers["urgent"]
    assert isinstance(status, jev.ChoiceAnswer)
    assert isinstance(urgent, jev.NoulAnswer)
    assert status.choice == "present"
    assert status.confidence == pytest.approx(0.72)
    assert urgent.noul == pytest.approx(0.99)


def test_parse_answers_drops_choice_without_confidence():
    # Confidence is what call sites gate on, so an answer lacking it is
    # unusable rather than merely incomplete.
    payload = {"answers": {"status": {"type": "choice", "choice": "present"}}}
    assert jev.parse_answers(payload) == {}


def test_parse_answers_keeps_good_answer_beside_malformed_one():
    payload = {
        "answers": {
            "broken": {"type": "choice", "choice": 42, "confidence": 0.9},
            "fine": {"type": "noul", "noul": 0.5},
        }
    }
    answers = jev.parse_answers(payload)
    assert set(answers) == {"fine"}


def test_parse_answers_ignores_unknown_primitive():
    payload = {"answers": {"rating": {"type": "score", "score": 1.0, "confidence": 0.9}}}
    assert jev.parse_answers(payload) == {}


def test_parse_answers_tolerates_non_dict_payload():
    assert jev.parse_answers("not json") == {}
    assert jev.parse_answers({"answers": []}) == {}


def test_parse_answers_drops_oversized_confidence_without_raising():
    # JSON has no integer width limit, so a literal this large satisfies
    # isinstance(x, int) and then overflows float(). It must be dropped like
    # any other malformed value, not raised out of the parser.
    payload = {
        "answers": {"status": {"type": "choice", "choice": "present", "confidence": 10**400}}
    }
    assert jev.parse_answers(payload) == {}


def test_parse_answers_drops_non_finite_confidence():
    # NaN and the infinities parse cleanly but would poison the threshold
    # comparison every call site gates on.
    for value in (float("nan"), float("inf"), float("-inf")):
        payload = {
            "answers": {"status": {"type": "choice", "choice": "present", "confidence": value}}
        }
        assert jev.parse_answers(payload) == {}, value


def test_parse_answers_drops_non_finite_noul():
    payload = {"answers": {"q": {"type": "noul", "noul": float("nan")}}}
    assert jev.parse_answers(payload) == {}


def test_parse_answers_drops_bad_probability_but_keeps_answer():
    # One unusable probability entry does not invalidate an otherwise good
    # answer -- confidence is the field call sites actually gate on.
    payload = {
        "answers": {
            "status": {
                "type": "choice",
                "choice": "present",
                "probabilities": {"present": 0.8, "shifted": 10**400},
                "confidence": 0.9,
            }
        }
    }
    answers = jev.parse_answers(payload)
    assert set(answers) == {"status"}
    answer = answers["status"]
    assert isinstance(answer, jev.ChoiceAnswer)
    assert answer.probabilities == {"present": 0.8}


def test_parse_noul_rejects_bool():
    # bool is an int subclass in Python; a raw True must not read as 1.0.
    payload = {"answers": {"q": {"type": "noul", "noul": True}}}
    assert jev.parse_answers(payload) == {}


# --- client construction -------------------------------------------------


def test_build_client_returns_none_when_disabled():
    assert jev.build_client(_settings(ARTEMIS_JEV_ENABLED=False)) is None


def test_build_client_returns_none_without_key():
    assert jev.build_client(_settings(TYPESAFE_API_KEY=None)) is None


def test_build_client_returns_none_with_empty_key():
    assert jev.build_client(_settings(TYPESAFE_API_KEY="")) is None


def test_build_client_unwraps_secret_str():
    class Secret:
        def get_secret_value(self):
            return "sk-unwrapped"

    client = jev.build_client(_settings(TYPESAFE_API_KEY=Secret()))
    assert client is not None
    assert client._api_key == "sk-unwrapped"


# --- request shape -------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_posts_documented_request_shape():
    captured = {}

    async def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(200, json={"answers": {}}, request=httpx.Request("POST", url))

    http = AsyncMock()
    http.post = fake_post
    client = jev.JevClient("sk-test", client=http)

    await client.ask("state text", {"q": jev.noul_question("is it true?")})

    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "jev-latest"
    assert captured["json"]["state"] == "state text"
    assert captured["json"]["questions"]["q"]["type"] == "noul"


@pytest.mark.asyncio
async def test_ask_short_circuits_without_questions():
    http = AsyncMock()
    client = jev.JevClient("sk-test", client=http)
    assert await client.ask("state", {}) == {}
    http.post.assert_not_awaited()


# --- best-effort boundary ------------------------------------------------


@pytest.mark.asyncio
async def test_module_ask_returns_none_without_client():
    assert await jev.ask(None, "state", {"q": jev.noul_question("?")}) is None


@pytest.mark.asyncio
async def test_module_ask_swallows_http_error():
    async def failing_post(*args, **kwargs):
        raise httpx.ConnectError("no route to host")

    http = AsyncMock()
    http.post = failing_post
    client = jev.JevClient("sk-test", client=http)

    assert await jev.ask(client, "state", {"q": jev.noul_question("?")}) is None


@pytest.mark.asyncio
async def test_module_ask_swallows_server_error_status():
    async def post_503(*args, **kwargs):
        return httpx.Response(503, request=httpx.Request("POST", "https://x"), text="down")

    http = AsyncMock()
    http.post = post_503
    client = jev.JevClient("sk-test", client=http)

    assert await jev.ask(client, "state", {"q": jev.noul_question("?")}) is None


@pytest.mark.asyncio
async def test_module_ask_propagates_cancellation():
    # Cancellation is control flow, not a Jev failure: swallowing it would
    # break structured concurrency for the whole device run.
    import asyncio

    async def cancelled_post(*args, **kwargs):
        raise asyncio.CancelledError()

    http = AsyncMock()
    http.post = cancelled_post
    client = jev.JevClient("sk-test", client=http)

    with pytest.raises(asyncio.CancelledError):
        await jev.ask(client, "state", {"q": jev.noul_question("?")})
