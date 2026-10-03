from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

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
async def test_model_save_preserves_jsonc_comments_and_api_version_query(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path)
    store.config_path.write_text(
        """{
  "default": {
    "provider": "openai",
    // Preserve this explanation when the model changes.
    "model": "gpt-4o",
    "api_base": "https://models.example.test/v1?api-version=2024-10-21",
    "fallback": {
      "provider": "openai",
      "model": "gpt-4o-mini",
      /* Preserve nested notes too. */
      "temperature": 0
    }
  },
  "nodes": {}
}
""",
        encoding="utf-8",
    )
    current = await store.read()
    changed_default = {**current["default"], "model": "gpt-4.1-mini"}

    saved = await store.save(
        expected_version=current["version"],
        default=changed_default,
        credentials={},
        base_urls={},
        actor_email="admin@example.test",
    )

    raw_config = store.config_path.read_text(encoding="utf-8")
    assert saved["default"]["model"] == "gpt-4.1-mini"
    assert "api-version=2024-10-21" in raw_config
    assert "Preserve this explanation" in raw_config
    assert "Preserve nested notes" in raw_config


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


@pytest.mark.asyncio
async def test_worker_settings_do_not_reload_dotenv_after_config_snapshot(tmp_path, monkeypatch):
    from apps.admin_console.services.task_queue_service import TaskQueueService

    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    monkeypatch.setenv("ARTEMIS_APP_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    store = make_store(tmp_path)
    store.config_path.write_text(config_text("old-live"), encoding="utf-8")
    before = await store.read()
    snapshot = await store.snapshot_for_spawn()

    try:
        assert snapshot.config_path.read_text(encoding="utf-8") == config_text("old-live")
        await store.save(
            expected_version=before["version"],
            default={**before["default"], "model": "later-model"},
            credentials={"openai": "SYNTHETIC-LATER-KEY"},
            base_urls={"openai": "https://later.example.test/v1"},
            actor_email="synthetic@example.test",
        )
        _, worker_environment = TaskQueueService._build_worker_invocation(
            {},
            "synthetic-run",
            None,
            "synthetic",
            "flash",
            MagicMock(lock_scope="synthetic"),
            base_environment=snapshot.environment,
        )
        script = (
            "import json, os; from artemis.config.settings import settings; "
            "print(json.dumps({'base': settings.OPENAI_BASE_URL, "
            "'key_set': settings.OPENAI_API_KEY is not None}))"
        )
        worker = subprocess.run(
            [sys.executable, "-B", "-c", script],
            env=worker_environment,
            capture_output=True,
            text=True,
        )

        assert worker.returncode == 0, worker.stderr
        assert json.loads(worker.stdout.strip().splitlines()[-1]) == {
            "base": None,
            "key_set": False,
        }
    finally:
        store.cleanup_snapshot(snapshot)


@pytest.mark.asyncio
async def test_spawn_snapshot_uses_current_managed_environment_after_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    monkeypatch.setenv("PATH", "synthetic-path")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC-OLD-KEY")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example.test/v1")
    store = make_store(
        tmp_path,
        b"OPENAI_API_KEY=SYNTHETIC-OLD-KEY\nOPENAI_BASE_URL=https://old.example.test/v1\n",
    )
    current = await store.read()
    await store.save(
        expected_version=current["version"],
        default=current["default"],
        credentials={"openai": None},
        base_urls={"openai": None},
        actor_email="admin@example.test",
    )

    snapshot = await store.snapshot_for_spawn()
    try:
        assert snapshot.environment["PATH"] == "synthetic-path"
        assert "OPENAI_API_KEY" not in snapshot.environment
        assert "OPENAI_BASE_URL" not in snapshot.environment
    finally:
        store.cleanup_snapshot(snapshot)


@pytest.mark.asyncio
async def test_config_save_works_without_os_fchmod(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path)
    current = await store.read()
    monkeypatch.delattr(config_store_module.os, "fchmod")

    saved = await store.save(
        expected_version=current["version"],
        default={**current["default"], "model": "windows-save"},
        credentials={},
        base_urls={},
        actor_email="synthetic@example.test",
    )

    assert saved["default"]["model"] == "windows-save"
    assert '"model": "windows-save"' in store.config_path.read_text(encoding="utf-8")


def test_directory_fsync_is_skipped_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module.os, "name", "nt")
    monkeypatch.setattr(
        config_store_module.os,
        "open",
        lambda *_args, **_kwargs: pytest.fail("directory open is unavailable on Windows"),
    )

    config_store_module._fsync_directory(tmp_path)


def test_settings_key_save_works_without_os_fchmod(tmp_path, monkeypatch):
    settings_module = importlib.import_module("artemis.config.settings")

    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_module, "get_env_file", lambda: env_file)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delattr(settings_module.os, "fchmod")

    settings_module.Settings().set_api_key("openai", "SYNTHETIC-WINDOWS-KEY", persist_to_env=True)

    assert "OPENAI_API_KEY='SYNTHETIC-WINDOWS-KEY'" in env_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_default_update_skips_comment_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    store = make_store(tmp_path)
    store.config_path.write_text(
        '/* Example: "default": {"provider":"google","model":"example"} */\n'
        '{"default":{"provider":"openai","model":"old-live",'
        '"fallback":{"provider":"openai","model":"fallback"}},"nodes":{}}',
        encoding="utf-8",
    )
    current = await store.read()
    updated = {**current["default"], "model": "new-choice"}

    saved = await store.save(
        expected_version=current["version"],
        default=updated,
        credentials={},
        base_urls={},
        actor_email="admin@example.test",
    )

    assert saved["default"]["model"] == "new-choice"
    assert '"model": "old-live"' not in store.config_path.read_text(encoding="utf-8")


def test_config_store_import_does_not_require_fcntl(monkeypatch):
    real_import = builtins.__import__

    def windows_import(name, *args, **kwargs):
        if name == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", windows_import)
    module_name = "review_windows_config_store"
    spec = importlib.util.spec_from_file_location(module_name, config_store_module.__file__)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert module.ConfigStore
    finally:
        sys.modules.pop(module_name, None)


def test_config_store_uses_windows_file_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    windows_lock = MagicMock(LK_LOCK=1, LK_UNLCK=2)
    monkeypatch.setitem(sys.modules, "msvcrt", windows_lock)
    monkeypatch.setattr(config_store_module.os, "name", "nt")

    with store._file_lock():
        pass

    assert [call.args[1:] for call in windows_lock.locking.call_args_list] == [(1, 1), (2, 1)]


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
