from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import apps.admin_console.services.config_store as config_store_module
from apps.admin_console.services.config_store import ConfigStore, ConfigStoreError


def config_text(model: str = "gpt-4o") -> str:
    return f'''{{
  // Preserved project note
  "default": {{
    "provider": "openai",
    "model": "{model}",
    "fallback": {{"provider": "openai", "model": "gpt-4o-mini"}}
  }},
  "presets": {{"kept": {{"provider": "google", "model": "gemini-3.7-flash"}}}},
  "nodes": {{}}
}}\n'''


def make_store(tmp_path: Path, env: bytes = b"") -> ConfigStore:
    config_path = tmp_path / "artemis.jsonc"
    env_path = tmp_path / ".env"
    config_path.write_text(config_text(), encoding="utf-8")
    if env:
        env_path.write_bytes(env)
    return ConfigStore(config_path, env_path)


@pytest.mark.asyncio
async def test_config_update_preserves_unedited_jsonc_and_masks_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-live-never-use-9876")
    store = make_store(tmp_path, b"OPENAI_API_KEY=synthetic-test-key-1234\nUNRELATED=keep\n")

    current = await store.read()
    current["default"]["model"] = "gpt-4.1-mini"
    updated = await store.save(
        expected_version=current["version"],
        default=current["default"],
        credentials={},
        base_urls={"openai": "https://models.example.test/v1"},
        actor_email="admin@example.test",
    )

    serialized = json.dumps(updated)
    config = store.config_path.read_text(encoding="utf-8")
    env = store.env_path.read_text(encoding="utf-8")
    assert updated["default"]["model"] == "gpt-4.1-mini"
    assert "Preserved project note" in config
    assert '"presets"' in config
    assert "UNRELATED=keep" in env
    assert "OPENAI_BASE_URL='https://models.example.test/v1'" in env
    assert "synthetic-test-key-1234" not in serialized
    assert "synthetic-live-never-use-9876" not in serialized
    assert updated["providers"][0]["key_preview"] == "****1234"
    assert store.audit_path.exists()
    assert "synthetic-test-key-1234" not in store.audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_config_snapshot_hides_secret_fields_and_url_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(
        tmp_path,
        b"OPENAI_BASE_URL='https://alice:url-secret@models.example.test/v1?token=query-secret#fragment'\n",
    )
    store.config_path.write_text(
        config_text().replace(
            '"model": "gpt-4o",',
            '"model": "gpt-4o",\n    "api_key": "synthetic-config-secret",\n    "api_base": "https://models.example.test/v1?token=inline-url-secret",',
        ),
        encoding="utf-8",
    )

    snapshot = await store.read()
    serialized = json.dumps(snapshot)

    assert "api_key" not in snapshot["default"]
    assert snapshot["providers"][0]["base_url"] == "https://models.example.test/v1"
    assert snapshot["default"]["api_base"] == "https://models.example.test/v1"
    assert all(
        secret not in serialized
        for secret in (
            "synthetic-config-secret",
            "inline-url-secret",
            "url-secret",
            "query-secret",
        )
    )

    with pytest.raises(ConfigStoreError):
        config_store_module._validate_base_url(
            "openai", "https://models.example.test/v1?token=inline-url-secret"
        )


@pytest.mark.asyncio
async def test_config_store_saves_and_clears_credentials_without_exposing_them(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    monkeypatch.setattr(
        type(config_store_module.settings), "set_api_key", lambda *_args, **_kwargs: None
    )
    store = make_store(tmp_path)
    current = await store.read()

    updated = await store.save(
        expected_version=current["version"],
        default=current["default"],
        credentials={"openai": "synthetic-new-key-4567"},
        base_urls={},
        actor_email="admin@example.test",
    )

    assert "OPENAI_API_KEY='synthetic-new-key-4567'" in store.env_path.read_text(encoding="utf-8")
    assert updated["providers"][0]["key_preview"] == "****4567"
    assert "synthetic-new-key-4567" not in json.dumps(updated)
    assert "synthetic-new-key-4567" not in store.audit_path.read_text(encoding="utf-8")

    cleared = await store.save(
        expected_version=updated["version"],
        default=updated["default"],
        credentials={"openai": None},
        base_urls={},
        actor_email="admin@example.test",
    )

    assert "OPENAI_API_KEY" not in store.env_path.read_text(encoding="utf-8")
    assert cleared["providers"][0]["configured"] is False
    assert cleared["providers"][0]["key_preview"] is None


@pytest.mark.asyncio
async def test_config_store_rejects_stale_versions_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path)
    original = await store.read()

    async def save_model(model: str):
        default = dict(original["default"])
        default["model"] = model
        return await store.save(
            expected_version=original["version"],
            default=default,
            credentials={},
            base_urls={},
            actor_email="admin@example.test",
        )

    results = await asyncio.gather(
        save_model("gpt-4.1-mini"), save_model("gpt-4.1"), return_exceptions=True
    )

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, ConfigStoreError)]
    assert len(errors) == 1
    assert errors[0].code == "config_conflict"


@pytest.mark.asyncio
async def test_config_store_rolls_back_env_when_jsonc_write_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path, b"OPENAI_BASE_URL=https://old.example.test/v1\n")
    original_config = store.config_path.read_bytes()
    original_env = store.env_path.read_bytes()
    current = await store.read()
    write = config_store_module._atomic_write
    should_fail = True

    def fail_once(path: Path, contents: bytes, mode: int | None = None):
        nonlocal should_fail
        if Path(path) == store.config_path and should_fail:
            should_fail = False
            raise OSError("synthetic config write failure")
        write(path, contents, mode)

    monkeypatch.setattr(config_store_module, "_atomic_write", fail_once)

    with pytest.raises(ConfigStoreError) as raised:
        await store.save(
            expected_version=current["version"],
            default=current["default"],
            credentials={},
            base_urls={"openai": "https://new.example.test/v1"},
            actor_email="admin@example.test",
        )

    assert raised.value.code == "config_unwritable"
    assert store.config_path.read_bytes() == original_config
    assert store.env_path.read_bytes() == original_env


@pytest.mark.asyncio
async def test_spawn_snapshot_is_stable_across_a_later_save(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path, b"SNAPSHOT_SETTING=before\n")
    current = await store.read()
    snapshot = await store.snapshot_for_spawn()
    old_config = snapshot.config_path.read_bytes()
    default = dict(current["default"])
    default["model"] = "gpt-4.1-mini"

    await store.save(
        expected_version=current["version"],
        default=default,
        credentials={},
        base_urls={},
        actor_email="admin@example.test",
    )

    assert snapshot.environment["SNAPSHOT_SETTING"] == "before"
    assert snapshot.config_path.read_bytes() == old_config
    assert store.config_path.read_bytes() != old_config
    ConfigStore.cleanup_snapshot(snapshot)
    assert not snapshot.config_path.exists()


def test_config_path_override_is_authoritative(tmp_path, monkeypatch):
    override = tmp_path / "explicit-artemis.jsonc"
    override.write_text(config_text(), encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(override))

    assert config_store_module._resolve_config_path() == override.resolve()


@pytest.mark.asyncio
async def test_invalid_runtime_config_is_rejected_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path, b"KEEP=original\n")
    current = await store.read()
    invalid = dict(current["default"])
    invalid["provider"] = "not-a-provider"
    original_config = store.config_path.read_bytes()
    original_env = store.env_path.read_bytes()

    with pytest.raises(ConfigStoreError) as raised:
        await store.save(
            expected_version=current["version"],
            default=invalid,
            credentials={},
            base_urls={},
            actor_email="admin@example.test",
        )

    assert raised.value.code == "config_invalid"
    assert store.config_path.read_bytes() == original_config
    assert store.env_path.read_bytes() == original_env
