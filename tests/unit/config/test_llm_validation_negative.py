"""Negative controls for LLM provider/key validation.

The agent tests supply a valid provider string and an opt-in placeholder key
(``dummy_llm_keys``) instead of weakening production validation. These tests pin the
behaviour that makes that setup necessary, so it cannot be relaxed unnoticed.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from artemis.config.llm import LLM
from artemis.config.settings import settings
from artemis.llm.router import ModelProvider


@pytest.mark.parametrize("bad", ["not-a-provider", "gpt", MagicMock(), object()])
def test_unknown_provider_value_is_rejected(bad):
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        ModelProvider.from_string(bad)


@pytest.mark.parametrize("good", ["google", "openai", "anthropic", None, ""])
def test_valid_or_default_provider_value_is_accepted(good):
    assert isinstance(ModelProvider.from_string(good), ModelProvider)


@pytest.mark.parametrize(
    ("provider", "setting", "env_name"),
    [
        ("openai", "OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("google", "GOOGLE_API_KEY", "GOOGLE_API_KEY"),
    ],
)
def test_missing_key_is_still_rejected(monkeypatch, provider, setting, env_name):
    monkeypatch.setattr(settings, setting, None)
    with pytest.raises(Exception, match=f"requires {env_name}"):
        LLM(provider=provider, model="any-model").validate_provider("Planner")


def test_dummy_llm_keys_satisfies_validation_only_when_requested(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    llm = LLM(provider="openai", model="any-model")
    with pytest.raises(Exception, match="requires OPENAI_API_KEY"):
        llm.validate_provider("Planner")


@pytest.mark.usefixtures("dummy_llm_keys")
def test_dummy_llm_keys_fixture_provides_non_real_placeholders():
    assert isinstance(settings.OPENAI_API_KEY, SecretStr)
    assert settings.OPENAI_API_KEY.get_secret_value() == "test-dummy-not-a-real-key"
    LLM(provider="openai", model="any-model").validate_provider("Planner")
