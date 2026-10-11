from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from apps.admin_console.services.config_store import (
    PROVIDER_BASE_URL_ENV,
    PROVIDER_KEY_ENV,
    ConfigStore,
    ConfigStoreError,
    _atomic_write,
    _mask,
    _safe_base_url,
    _validate_base_url,
    get_config_store,
)
from apps.admin_console.services.model_discovery import BASES, ModelDiscovery
from artemis.config.llm import _expand_default_into_nodes
from artemis.config.settings import is_placeholder_key, settings
from artemis.utils.file import load_jsonc
from artemis.utils.redaction import REDACTED, redact, register_secrets

SLOTS = ("openai", "google", "anthropic", "xai", "openrouter")
LABELS = {
    "openai": "OpenAI",
    "google": "Google",
    "anthropic": "Anthropic",
    "xai": "xAI",
    "openrouter": "OpenRouter",
}
SUB2API = "https://congvc-c00.taila6fa8a.ts.net:31344"


def _version(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _fingerprint(slot: dict) -> tuple:
    return slot["key"], slot["base_url"], slot["enabled"]


def _safe_error(text: str, keys: list[str]) -> str:
    for key in sorted((key for key in keys if key), key=len, reverse=True):
        for value in (key, quote(key, safe=""), json.dumps(key)[1:-1]):
            text = text.replace(value, REDACTED)
    return redact(text)[:1000]


class ProviderSettings:
    def __init__(self, store: ConfigStore, *, transport=None):
        self.store = store
        self.path = store.env_path.parent / "artemis-providers.json"
        self.transport = transport

    def _write(self, document: dict) -> None:
        try:
            _atomic_write(self.path, json.dumps(document, sort_keys=True).encode(), 0o600)
        except OSError as exc:
            raise ConfigStoreError(
                "config_unwritable", "Provider settings could not be saved.", 409
            ) from exc

    def _load_locked(self, config_bytes: bytes, environment: dict[str, str]) -> dict:
        try:
            document = json.loads(self.path.read_bytes()) if self.path.exists() else {"slots": {}}
            operator = _expand_default_into_nodes(load_jsonc(io.StringIO(config_bytes.decode())))[
                "operator"
            ]
            changed = False
            for provider in SLOTS:
                key = next(
                    (
                        environment.get(name)
                        for name in PROVIDER_KEY_ENV[provider]
                        if not is_placeholder_key(environment.get(name))
                    ),
                    "",
                )
                base = environment.get(PROVIDER_BASE_URL_ENV[provider]) or BASES[provider]
                if operator["provider"] == provider and operator.get("api_base"):
                    base = operator["api_base"]
                existing = document["slots"].get(provider)
                if existing and existing["saved"]:
                    continue
                _validate_base_url(provider, base)
                slot = {
                    "key": key,
                    "base_url": base.rstrip("/"),
                    "enabled": bool(key),
                    "saved": False,
                    "models": [],
                    "last_sync": None,
                    "sync_error": None,
                    "last_test": None,
                    "scan_at": None,
                }
                if existing and _fingerprint(existing) == _fingerprint(slot):
                    continue
                document["slots"][provider] = slot
                changed = True
            if changed:
                self._write(document)
            document["slots"] = {provider: document["slots"][provider] for provider in SLOTS}
            register_secrets([slot["key"] for slot in document["slots"].values()])
            return document
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigStoreError(
                "config_invalid", "Provider settings could not be read.", 409
            ) from exc

    def _state(self) -> dict:
        with self.store._file_lock():
            config_bytes, environment = self.store._runtime_inputs(provider_slots=False)
            return self._load_locked(config_bytes, environment)

    def _public(self, document: dict) -> dict:
        return {
            "version": _version(document),
            "slots": [
                {
                    "provider": provider,
                    "label": LABELS[provider],
                    "base_url": _safe_base_url(slot["base_url"]),
                    "key_preview": _mask(slot["key"]),
                    "configured": bool(slot["key"]),
                    "enabled": slot["enabled"],
                    "saved": slot["saved"],
                    "last_test": slot["last_test"],
                    "model_count": len(slot["models"]),
                    "last_sync": slot["last_sync"],
                    "sync_error": slot["sync_error"],
                    "presets": [
                        {"name": "Official", "url": BASES[provider]},
                        {
                            "name": "sub2api (C00)",
                            "url": SUB2API + ("" if provider in {"google", "anthropic"} else "/v1"),
                        },
                    ],
                }
                for provider, slot in document["slots"].items()
            ],
        }

    async def read(self) -> dict:
        return self._public(await asyncio.to_thread(self._state))

    def saved_slots_locked(self) -> dict:
        if not self.path.exists():
            return {}
        config_bytes, environment = self.store._runtime_inputs(provider_slots=False)
        document = self._load_locked(config_bytes, environment)
        return {provider: slot for provider, slot in document["slots"].items() if slot["saved"]}

    async def activate_runtime(self) -> None:
        document = await asyncio.to_thread(self._state)
        for provider, slot in document["slots"].items():
            if not slot["saved"]:
                continue
            for name in PROVIDER_KEY_ENV[provider]:
                setattr(settings, name, SecretStr(slot["key"] if slot["enabled"] else ""))
            setattr(settings, PROVIDER_BASE_URL_ENV[provider], slot["base_url"])

    async def save(
        self,
        provider: str,
        version: str,
        *,
        actor: str | None,
        key: str | None = None,
        base_url: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        self._require_slot(provider)
        return self._public(
            await asyncio.to_thread(self._save, provider, version, actor, key, base_url, enabled)
        )

    def _save(self, provider, version, actor, key, base_url, enabled):
        with self.store._file_lock():
            config_bytes, environment = self.store._runtime_inputs(provider_slots=False)
            document = self._load_locked(config_bytes, environment)
            if _version(document) != version:
                raise ConfigStoreError(
                    "config_conflict", "Provider settings changed. Reload and retry.", 409
                )
            slot = document["slots"][provider]
            previous = _fingerprint(slot)
            if base_url is not None:
                _validate_base_url(provider, base_url)
                if not base_url.strip():
                    raise ConfigStoreError("config_invalid", "A provider URL is required.")
                slot["base_url"] = base_url.strip().rstrip("/")
            if key is not None:
                if len(key) > 4096 or any(ord(char) < 33 for char in key):
                    raise ConfigStoreError(
                        "config_invalid", "A key must not contain whitespace or control characters."
                    )
                slot["key"] = key
            if enabled is not None:
                slot["enabled"] = enabled
            if slot["enabled"] and not slot["key"]:
                raise ConfigStoreError("config_invalid", "An enabled provider needs a key.")
            if slot["key"] and slot["key"] in slot["base_url"]:
                raise ConfigStoreError(
                    "config_invalid", "The provider URL must not contain the API key."
                )
            register_secrets([slot["key"]])
            slot["saved"] = True
            if previous[:2] != _fingerprint(slot)[:2]:
                slot.update(
                    models=[], last_sync=None, last_test=None, sync_error=None, scan_at=None
                )
            old_bytes = self.path.read_bytes()
            self._write(document)
            if key is not None and key != previous[0]:
                try:
                    audit = {
                        "event": "provider_key_changed",
                        "actor_email": actor,
                        "timestamp": time.time(),
                    }
                    descriptor = os.open(
                        self.store.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
                    )
                    try:
                        os.write(descriptor, (json.dumps(audit) + "\n").encode())
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError as exc:
                    _atomic_write(self.path, old_bytes, 0o600)
                    raise ConfigStoreError(
                        "config_unwritable",
                        "The key change could not be audited; previous settings were restored.",
                        409,
                    ) from exc
            return document

    @staticmethod
    def _require_slot(provider: str) -> None:
        if provider not in SLOTS:
            raise ConfigStoreError("provider_unknown", "Unknown provider slot.", 404)

    async def test(self, provider: str) -> dict:
        return await self._action(provider, test=True)

    async def sync(self, provider: str) -> dict:
        return await self._action(provider, test=False)

    async def _action(self, provider: str, *, test: bool) -> dict:
        self._require_slot(provider)
        document = await asyncio.to_thread(self._state)
        slot = document["slots"][provider]
        if not slot["key"]:
            raise ConfigStoreError("provider_key_missing", "Save a provider key first.")
        started = time.monotonic()
        error = None
        rows = None
        keys = [entry["key"] for entry in document["slots"].values()]
        try:
            async with httpx.AsyncClient(
                timeout=10, transport=self.transport, follow_redirects=False, trust_env=False
            ) as client:
                async with asyncio.timeout(30):
                    rows = await ModelDiscovery(self.store)._scan(
                        client, provider, slot["key"], slot["base_url"]
                    )
                    if any(
                        key and (key in row["model"] or quote(key, safe="") in row["model"])
                        for key in keys
                        for row in rows
                    ):
                        raise ValueError("The model list contains credentials.")
                    if test:
                        if not rows:
                            raise ValueError("The model list has no usable models.")
                        config_bytes, _environment = await self.store.model_discovery_inputs()
                        operator = _expand_default_into_nodes(
                            load_jsonc(io.StringIO(config_bytes.decode()))
                        )["operator"]
                        models = [row["model"] for row in rows]
                        preferred = next(
                            (
                                model
                                for model in models
                                if model == operator["model"] and provider == operator["provider"]
                            ),
                            None,
                        )
                        model = preferred or next(
                            (
                                model
                                for model in models
                                if model.rsplit("/", 1)[-1].startswith(
                                    (
                                        "gpt-",
                                        "chatgpt-",
                                        "o1",
                                        "o3",
                                        "o4",
                                        "gemini-",
                                        "claude-",
                                        "grok-",
                                    )
                                )
                            ),
                            models[0],
                        )
                        await self._probe(client, provider, slot, model)
        except httpx.HTTPStatusError as exc:
            message = ""
            try:
                payload = exc.response.json()
                value = payload.get("error", payload)
                message = value.get("message", "") if isinstance(value, dict) else str(value)
            except (ValueError, AttributeError):
                pass
            error = _safe_error(
                f"{LABELS[provider]} protocol request failed (HTTP {exc.response.status_code}). {message}",
                keys,
            )
        except (TimeoutError, httpx.HTTPError):
            error = f"{LABELS[provider]} protocol request timed out or could not connect."
        except (ValueError, TypeError, KeyError):
            error = f"The URL did not return a valid {LABELS[provider]} protocol response."
        elapsed = round((time.monotonic() - started) * 1000)
        result = await asyncio.to_thread(self._finish, provider, slot, test, rows, error, elapsed)
        return self._public(result)

    async def _probe(self, client, provider, slot, model):
        headers = {"Authorization": f"Bearer {slot['key']}"}
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "max_completion_tokens": 1,
        }
        path = "/chat/completions"
        if provider == "google":
            headers = {"x-goog-api-key": slot["key"]}
            path = f"/v1beta/models/{quote(model, safe='')}:generateContent"
            body = {
                "contents": [{"parts": [{"text": "Reply OK."}]}],
                "generationConfig": {"maxOutputTokens": 1},
            }
        elif provider == "anthropic":
            headers = {"x-api-key": slot["key"], "anthropic-version": "2023-06-01"}
            path = "/v1/messages"
            body.pop("max_completion_tokens")
            body["max_tokens"] = 1
        response = await client.post(slot["base_url"] + path, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Wrong protocol")
        if provider == "google":
            candidates = payload.get("candidates")
            valid = (
                isinstance(candidates, list)
                and bool(candidates)
                and all(
                    isinstance(candidate, dict)
                    and (
                        isinstance(candidate.get("content"), dict)
                        or isinstance(candidate.get("finishReason"), str)
                    )
                    for candidate in candidates
                )
            )
        elif provider == "anthropic":
            content = payload.get("content")
            valid = (
                payload.get("type") == "message"
                and isinstance(content, list)
                and all(
                    isinstance(block, dict) and isinstance(block.get("type"), str)
                    for block in content
                )
            )
        else:
            choices = payload.get("choices")
            valid = (
                isinstance(choices, list)
                and bool(choices)
                and all(
                    isinstance(choice, dict) and isinstance(choice.get("message"), dict)
                    for choice in choices
                )
            )
        if not valid:
            raise ValueError("Wrong protocol")

    def _finish(self, provider, previous, test, rows, error, elapsed):
        with self.store._file_lock():
            config_bytes, environment = self.store._runtime_inputs(provider_slots=False)
            document = self._load_locked(config_bytes, environment)
            slot = document["slots"][provider]
            if _fingerprint(slot) != _fingerprint(previous):
                raise ConfigStoreError(
                    "config_conflict", "Provider settings changed during the request. Retry.", 409
                )
            now = time.time()
            if test:
                slot["last_test"] = {
                    "ok": error is None,
                    "latency_ms": elapsed,
                    "error": error,
                    "at": now,
                }
            else:
                slot["sync_error"] = error
                slot["scan_at"] = now
                if error is None:
                    slot["models"] = [row["model"] for row in rows]
                    slot["last_sync"] = now
            self._write(document)
            return document

    async def catalog(self, default: dict, deny: list[str]) -> dict:
        document = await asyncio.to_thread(self._state)
        await asyncio.gather(
            *(
                self.sync(provider)
                for provider, slot in document["slots"].items()
                if not slot["saved"]
                and slot["enabled"]
                and (slot["scan_at"] is None or time.time() - slot["scan_at"] >= 600)
            )
        )
        document = await asyncio.to_thread(self._state)
        return {
            "models": [
                {
                    "provider": provider,
                    "model": model,
                    "source": slot["base_url"]
                    + {"google": "/v1beta/models", "anthropic": "/v1/models"}.get(
                        provider, "/models"
                    ),
                }
                for provider, slot in document["slots"].items()
                if slot["enabled"]
                for model in slot["models"]
                if model not in deny and f"{provider}/{model}" not in deny
            ],
            "default": default,
            "errors": [
                {"provider": provider, "error": slot["sync_error"]}
                for provider, slot in document["slots"].items()
                if slot["enabled"] and slot["sync_error"]
            ],
        }

    def apply_runtime_locked(
        self, config_bytes: bytes, environment: dict[str, str]
    ) -> tuple[bytes, dict[str, str]]:
        document = self._load_locked(config_bytes, environment)
        if not any(slot["saved"] for slot in document["slots"].values()):
            return config_bytes, environment
        config = load_jsonc(io.StringIO(config_bytes.decode()))
        for provider, slot in document["slots"].items():
            if not slot["saved"]:
                continue
            for name in PROVIDER_KEY_ENV[provider]:
                environment[name] = slot["key"] if slot["enabled"] else ""
            environment[PROVIDER_BASE_URL_ENV[provider]] = slot["base_url"]

        def update(value):
            if isinstance(value, dict):
                slot = document["slots"].get(value.get("provider"))
                if slot and slot["saved"]:
                    value["api_base"] = slot["base_url"]
                for nested in value.values():
                    update(nested)
            elif isinstance(value, list):
                for nested in value:
                    update(nested)

        update(config)
        return json.dumps(config).encode(), environment


def get_provider_settings() -> ProviderSettings:
    return ProviderSettings(get_config_store())
