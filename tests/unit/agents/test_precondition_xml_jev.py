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

"""Tests for Jev adjudication of the XML safety net.

The invariant these protect: Jev may only refine or overturn a *below
threshold* heuristic verdict, and only when it is both reachable and
confident. In every other case the pre-existing heuristic decides, so a run on
a host with no TypeSafe access behaves exactly as it did before.
"""

import pytest

from artemis.agents.validator import precondition_xml as px
from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.services import jev

_ELEMENTS = [
    {
        "resource-id": "com.example:id/submit",
        "text": "Submit",
        "class": "android.widget.Button",
        "bounds": "[100,200][300,260]",
    },
    {
        "resource-id": "com.example:id/banner",
        "text": "Advert",
        "class": "android.widget.FrameLayout",
        "bounds": "[0,0][1080,180]",
    },
]

_BEST = {
    "element": _ELEMENTS[0],
    "center": [200, 230],
    "bounds": [100, 200, 300, 260],
    "text": "Submit",
    "resource_id": "com.example:id/submit",
    "score": 0.41,
    "identity_score": 0.4,
    "distance": 12.0,
    "text_score": 0.5,
    "id_match": True,
}


def _action_item():
    return {
        "action": "tap",
        "coordinates": [200, 230],
        "target_text": "Submit",
        "target_resource_id": "com.example:id/submit",
        "target_bounds": [100, 200, 300, 260],
    }


async def _classify(monkeypatch, answers, *, client=object()):
    """Runs the adjudicator with a stubbed Jev client and canned answers."""
    monkeypatch.setattr(px.jev, "build_client", lambda _settings: client)

    async def fake_ask(_client, _state, _questions):
        return answers

    monkeypatch.setattr(px.jev, "ask", fake_ask)
    return await px._classify_failure_with_jev(
        _ELEMENTS,
        _action_item(),
        _BEST,
        target_text="Submit",
        target_bounds=[100, 200, 300, 260],
        target_resource_id="com.example:id/submit",
    )


@pytest.mark.asyncio
async def test_returns_none_when_jev_is_disabled(monkeypatch):
    # The default posture on every host without TypeSafe access.
    monkeypatch.setattr(px.jev, "build_client", lambda _settings: None)
    result = await px._classify_failure_with_jev(
        _ELEMENTS,
        _action_item(),
        _BEST,
        target_text="Submit",
        target_bounds=[100, 200, 300, 260],
        target_resource_id="com.example:id/submit",
    )
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_call_fails(monkeypatch):
    assert await _classify(monkeypatch, None) is None


@pytest.mark.asyncio
async def test_returns_none_on_low_confidence(monkeypatch):
    answer = jev.ChoiceAnswer(choice="present", probabilities={"present": 0.5}, confidence=0.2)
    assert await _classify(monkeypatch, {"target_status": answer}) is None


@pytest.mark.asyncio
async def test_returns_none_on_unknown_label(monkeypatch):
    answer = jev.ChoiceAnswer(choice="banana", probabilities={}, confidence=0.99)
    assert await _classify(monkeypatch, {"target_status": answer}) is None


@pytest.mark.asyncio
async def test_present_verdict_overturns_heuristic_block(monkeypatch):
    answer = jev.ChoiceAnswer(choice="present", probabilities={"present": 0.91}, confidence=0.88)
    verdict = await _classify(monkeypatch, {"target_status": answer})
    assert verdict is not None
    passed, category, reason = verdict
    assert passed is True
    assert category == ValidationErrorCategory.NONE
    assert reason == ""


@pytest.mark.asyncio
async def test_shifted_verdict_maps_to_category_and_evidence(monkeypatch):
    answer = jev.ChoiceAnswer(choice="shifted", probabilities={"shifted": 0.8}, confidence=0.79)
    monkeypatch.setattr(px.jev, "build_client", lambda _settings: object())

    async def fake_ask(_client, _state, _questions):
        return {"target_status": answer}

    monkeypatch.setattr(px.jev, "ask", fake_ask)

    item = _action_item()
    verdict = await px._classify_failure_with_jev(
        _ELEMENTS,
        item,
        _BEST,
        target_text="Submit",
        target_bounds=[100, 200, 300, 260],
        target_resource_id="com.example:id/submit",
    )
    assert verdict is not None
    passed, category, reason = verdict
    assert passed is False
    assert category == ValidationErrorCategory.TARGET_SHIFTED
    # The Operator's incident needs the new position, same as the heuristic path.
    assert item["safety_net_evidence"]["new_center"] == [200, 230]


@pytest.mark.asyncio
async def test_occupied_and_disappeared_map_to_their_categories(monkeypatch):
    occupied = jev.ChoiceAnswer(choice="occupied", probabilities={}, confidence=0.9)
    verdict = await _classify(monkeypatch, {"target_status": occupied})
    assert verdict is not None
    assert verdict[1] == ValidationErrorCategory.TARGET_OCCUPIED

    gone = jev.ChoiceAnswer(choice="disappeared", probabilities={}, confidence=0.9)
    verdict = await _classify(monkeypatch, {"target_status": gone})
    assert verdict is not None
    assert verdict[1] == ValidationErrorCategory.TARGET_DISAPPEARED


def test_state_text_includes_target_and_candidates():
    state = px._build_jev_state(
        _action_item(),
        _BEST,
        _ELEMENTS,
        target_text="Submit",
        target_bounds=[100, 200, 300, 260],
        target_resource_id="com.example:id/submit",
    )
    # The best candidate must be described, not just referenced by score.
    assert "com.example:id/submit" in state
    assert "Submit" in state
    # Other on-screen elements give Jev the context to spot an occluder.
    assert "com.example:id/banner" in state
    assert "heuristic_similarity_score=0.410" in state


def test_labels_are_within_vendor_cardinality_limit():
    assert len(px.JEV_TARGET_LABELS) <= jev.MAX_CHOICE_CARDINALITY
    # The question builder must accept the live label set.
    jev.choice_question("status?", px.JEV_TARGET_LABELS)
