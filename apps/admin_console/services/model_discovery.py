from __future__ import annotations

import asyncio
import hashlib
import io
import time
from typing import Any

import httpx

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.services.config_store import (
    PROVIDER_BASE_URL_ENV,
    PROVIDER_KEY_ENV,
    ConfigStore,
    _validate_base_url,
    get_config_store,
)
from artemis.config.llm import _expand_default_into_nodes
from artemis.config.settings import is_placeholder_key
from artemis.utils.file import load_jsonc

BASES = {
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com",
    "anthropic": "https://api.anthropic.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "xai": "https://api.x.ai/v1",
}
DEFAULT_DENY = ["codex-auto-review", "gpt-reserve"]


def parse_models(provider: str, payload: Any) -> list[str]:
    rows = (
        payload.get("models" if provider == "google" else "data")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list):
        raise ValueError("Invalid model list")
    models = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid model row")
        if provider == "google" and "generateContent" not in row.get(
            "supportedGenerationMethods", []
        ):
            continue
        model = row.get("name" if provider == "google" else "id")
        if provider == "google" and isinstance(model, str):
            model = model.removeprefix("models/")
        if (
            not isinstance(model, str)
            or not model
            or len(model) > 256
            or any(ord(char) < 33 for char in model)
        ):
            raise ValueError("Invalid model id")
        models.add(model)
    return sorted(models)


class ModelDiscovery:
    TTL = 600

    def __init__(self, store: ConfigStore | None = None, *, transport=None):
        self.store = store
        self.transport = transport
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict[str, Any]] = {}

    async def _scan(self, client, provider, key, base):
        path = {"google": "/v1beta/models", "anthropic": "/v1/models"}.get(provider, "/models")
        source = base.rstrip("/") + path
        headers = {"Authorization": f"Bearer {key}"}
        params: dict[str, str | int] = {}
        if provider == "google":
            headers = {"x-goog-api-key": key}
            params = {"pageSize": 1000}
        elif provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            params = {"limit": 1000}
        models = set()
        for _page in range(20):
            response = await client.get(source, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()
            models.update(parse_models(provider, payload))
            if provider == "google" and payload.get("nextPageToken"):
                params["pageToken"] = payload["nextPageToken"]
            elif provider == "anthropic" and payload.get("has_more"):
                if not payload.get("last_id"):
                    raise ValueError("Invalid pagination")
                params["after_id"] = payload["last_id"]
            else:
                return [
                    {"provider": provider, "model": model, "source": source}
                    for model in sorted(models)
                ]
        raise ValueError("Model list exceeds pagination limit")

    async def catalog(self) -> dict[str, Any]:
        async with self._lock:
            config_bytes, environment = await (
                self.store or get_config_store()
            ).model_discovery_inputs()
            config = load_jsonc(io.StringIO(config_bytes.decode("utf-8")))
            operator = _expand_default_into_nodes(config)["operator"]
            default = {"provider": operator["provider"], "model": operator["model"]}
            deny = config.get("model_discovery", {}).get("deny", DEFAULT_DENY)
            if not isinstance(deny, list) or not all(isinstance(model, str) for model in deny):
                raise ValueError("model_discovery.deny must be a list of model IDs")
            active = {}
            for provider, base in BASES.items():
                key = next(
                    (
                        environment.get(name)
                        for name in PROVIDER_KEY_ENV[provider]
                        if not is_placeholder_key(environment.get(name))
                    ),
                    None,
                )
                if not key:
                    continue
                base = environment.get(PROVIDER_BASE_URL_ENV[provider]) or base
                if operator["provider"] == provider and operator.get("api_base"):
                    base = operator["api_base"]
                _validate_base_url(provider, base)
                active[provider] = (key, base.rstrip("/"))
            self._cache = {
                provider: entry for provider, entry in self._cache.items() if provider in active
            }
            async with httpx.AsyncClient(
                timeout=10, transport=self.transport, follow_redirects=False
            ) as client:
                await asyncio.gather(
                    *(
                        self._refresh(client, provider, key, base)
                        for provider, (key, base) in active.items()
                    )
                )
            return {
                "models": [
                    row
                    for entry in self._cache.values()
                    for row in entry["rows"]
                    if row["model"] not in deny and f"{row['provider']}/{row['model']}" not in deny
                ],
                "default": default,
                "errors": [
                    {"provider": provider, "error": entry["error"]}
                    for provider, entry in self._cache.items()
                    if entry["error"]
                ],
            }

    async def _refresh(self, client, provider, key, base):
        fingerprint = hashlib.sha256(f"{key}\n{base}".encode()).hexdigest()
        previous = self._cache.get(provider)
        if (
            previous
            and previous["fingerprint"] == fingerprint
            and time.monotonic() - previous["time"] < self.TTL
        ):
            return
        rows = previous["rows"] if previous and previous["fingerprint"] == fingerprint else []
        error = None
        try:
            rows = await self._scan(client, provider, key, base)
        except httpx.HTTPStatusError as exc:
            error = f"Model scan failed (HTTP {exc.response.status_code})."
        except (httpx.HTTPError, ValueError, TypeError):
            error = "Model scan failed. The last successful list is retained."
        self._cache[provider] = {
            "fingerprint": fingerprint,
            "time": time.monotonic(),
            "rows": rows,
            "error": error,
        }

    async def resolve(self, selection: dict[str, str] | None) -> dict[str, str]:
        if selection is None:
            config_bytes, _environment = await (
                self.store or get_config_store()
            ).model_discovery_inputs()
            operator = _expand_default_into_nodes(
                load_jsonc(io.StringIO(config_bytes.decode("utf-8")))
            )["operator"]
            return {"provider": operator["provider"], "model": operator["model"]}
        catalog = await self.catalog()
        if not any(
            all(row[key] == selection.get(key) for key in ("provider", "model"))
            for row in catalog["models"]
        ):
            raise AdminAPIError(
                400,
                "The selected model is not available.",
                "model_not_available",
                "Refresh the model list or use the server default.",
            )
        return {"provider": selection["provider"], "model": selection["model"]}


model_discovery = ModelDiscovery()
