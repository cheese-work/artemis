from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from artemis.config.llm import LLMConfig, _expand_default_into_nodes
from artemis.config.paths import ROOT_DIR, get_config_path, get_env_file
from artemis.config.settings import SERVICE_ENVIRONMENT_KEYS, settings
from artemis.utils.file import load_jsonc

logger = logging.getLogger(__name__)

PROVIDER_KEY_ENV = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPEN_ROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "ocr": ("OCR_API_KEY", "VISION_API_KEY"),
}
PROVIDER_BASE_URL_ENV = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
}
PROVIDERS = ("openai", "google", "anthropic", "openrouter", "xai", "vertexai", "ocr")
_SENSITIVE_CONFIG_KEYS = {
    "key",
    "apikey",
    "secret",
    "password",
    "token",
    "authorization",
    "privatekey",
    "accesstoken",
    "refreshtoken",
}


class ConfigStoreError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 400):
        self.code = code
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class SpawnConfigSnapshot:
    environment: dict[str, str]
    config_path: Path


def _resolve_config_path() -> Path:
    configured_path = os.getenv("ARTEMIS_ARTEMIS_JSONC", "").strip()
    try:
        candidate = (
            Path(configured_path).expanduser()
            if configured_path
            else get_config_path("artemis.jsonc")
        )
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError, FileNotFoundError) as exc:
        raise ConfigStoreError(
            "config_unwritable",
            "The active artemis.jsonc file cannot be resolved; check ARTEMIS_ARTEMIS_JSONC.",
            409,
        ) from exc
    return resolved


def _sha(config_bytes: bytes, env_bytes: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(len(config_bytes).to_bytes(8, "big"))
    digest.update(config_bytes)
    digest.update(len(env_bytes).to_bytes(8, "big"))
    digest.update(env_bytes)
    return digest.hexdigest()


def _skip_jsonc_trivia(text: str, index: int) -> int:
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ConfigStoreError("config_invalid", "artemis.jsonc has an unclosed comment.")
            index = end + 2
        else:
            return index
    return index


def _jsonc_string_end(text: str, start: int) -> int:
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return index
    raise ConfigStoreError("config_invalid", "artemis.jsonc has an unterminated string.")


def _matching_object_end(text: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                break
            index = end + 2
            continue
        if char == '"':
            index = _jsonc_string_end(text, index) + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1
        index += 1
    raise ConfigStoreError("config_invalid", "artemis.jsonc default object is not closed.")


def _find_default_block(text: str) -> tuple[int, int]:
    root = _skip_jsonc_trivia(text, 0)
    if root == len(text) or text[root] != "{":
        raise ConfigStoreError("config_invalid", "artemis.jsonc must contain a default object.")

    containers = ["{"]
    previous = "{"
    index = root + 1
    while index < len(text):
        index = _skip_jsonc_trivia(text, index)
        if index >= len(text):
            break
        char = text[index]
        if char == '"':
            end = _jsonc_string_end(text, index)
            if len(containers) == 1 and previous in {"{", ","}:
                try:
                    key = json.loads(text[index : end + 1])
                except json.JSONDecodeError as exc:
                    raise ConfigStoreError(
                        "config_invalid", "artemis.jsonc has an invalid key."
                    ) from exc
                if key == "default":
                    separator = _skip_jsonc_trivia(text, end + 1)
                    value = _skip_jsonc_trivia(text, separator + 1)
                    if separator >= len(text) or text[separator] != ":":
                        raise ConfigStoreError(
                            "config_invalid", "artemis.jsonc default value is invalid."
                        )
                    if value >= len(text) or text[value] != "{":
                        raise ConfigStoreError(
                            "config_invalid", "artemis.jsonc default must be an object."
                        )
                    return _matching_object_end(text, value)
            index = end + 1
            previous = '"'
            continue
        if char in "{[":
            containers.append(char)
        elif char in "}]":
            if containers:
                containers.pop()
            if not containers:
                break
        previous = char
        index += 1

    raise ConfigStoreError("config_invalid", "artemis.jsonc must contain a default object.")


def _replace_default(text: str, default_config: dict[str, Any]) -> str:
    start, end = _find_default_block(text)
    line_start = text.rfind("\n", 0, start) + 1
    indentation = re.match(r"\s*", text[line_start:start]).group(0)
    replacement = json.dumps(default_config, indent=2, ensure_ascii=False)
    replacement_lines = replacement.splitlines()
    replacement = (
        replacement_lines[0]
        + "\n"
        + "\n".join(f"{indentation}{line}" for line in replacement_lines[1:])
    )
    return text[:start] + replacement + text[end:]


def _validate_config_text(text: str) -> dict[str, Any]:
    try:
        config_dict = load_jsonc(io.StringIO(text))
        expanded = _expand_default_into_nodes(config_dict)
        LLMConfig.model_validate(expanded)
        return config_dict
    except Exception as exc:
        raise ConfigStoreError("config_invalid", "The model configuration is invalid.") from exc


def _set_dotenv_values(original: bytes, updates: dict[str, str | None]) -> bytes:
    text = original.decode("utf-8") if original else ""
    for key, value in updates.items():
        pattern = re.compile(rf"^(?:export\s+)?{re.escape(key)}\s*=.*(?:\n|$)", re.MULTILINE)
        text = pattern.sub("", text)
        if value is not None:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{key}='{escaped}'\n"
    return text.encode("utf-8")


def _atomic_write(path: Path, contents: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode if mode is not None else existing_mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return f"****{value[-4:]}" if len(value) >= 4 else "****"


def _safe_config_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized_key in _SENSITIVE_CONFIG_KEYS:
                continue
            if normalized_key in {"apibase", "baseurl"} and isinstance(item, str):
                safe[key] = _safe_base_url(item)
            else:
                safe[key] = _safe_config_value(item)
        return safe
    if isinstance(value, list):
        return [_safe_config_value(item) for item in value]
    return value


def _safe_base_url(value: str | None) -> str | None:
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        if not (parsed.username or parsed.password or parsed.query or parsed.fragment):
            return value
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _source_for(key: str, env_values: dict[str, str | None]) -> str:
    if key in SERVICE_ENVIRONMENT_KEYS:
        return "service environment (read-only)"
    if key in env_values and env_values[key]:
        return ".env"
    return "not configured"


def _validate_base_url(provider: str, value: str | None) -> None:
    if value is None or not value.strip():
        return
    try:
        parsed = urlsplit(value.strip())
    except ValueError as exc:
        raise ConfigStoreError("config_invalid", f"{provider} base URL is invalid.") from exc
    host = (parsed.hostname or "").casefold()
    is_local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".ts.net")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise ConfigStoreError(
            "config_invalid",
            f"{provider} base URL must use HTTPS except for localhost and tailnet hosts.",
        )
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigStoreError("config_invalid", f"{provider} base URL is invalid.")


class ConfigStore:
    def __init__(self, config_path: Path, env_path: Path):
        self.config_path = Path(config_path).resolve()
        self.env_path = Path(env_path).resolve()
        self._async_lock = asyncio.Lock()
        self.lock_path = self.env_path.parent / ".artemis-config.lock"
        self.audit_path = self.env_path.parent / "artemis-admin-audit.jsonl"

    @classmethod
    def from_environment(cls) -> ConfigStore:
        return cls(_resolve_config_path(), get_env_file())

    @contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_bytes(self) -> tuple[bytes, bytes]:
        try:
            return (
                self.config_path.read_bytes(),
                self.env_path.read_bytes() if self.env_path.exists() else b"",
            )
        except OSError as exc:
            raise ConfigStoreError(
                "config_unwritable", "Unable to read the active model configuration.", 409
            ) from exc

    def _read_snapshot_locked(self) -> dict[str, Any]:
        config_bytes, env_bytes = self._read_bytes()
        try:
            config_text = config_bytes.decode("utf-8")
            config_dict = _validate_config_text(config_text)
            env_values = {
                key: value
                for key, value in dotenv_values(
                    stream=io.StringIO(env_bytes.decode("utf-8"))
                ).items()
            }
        except UnicodeDecodeError as exc:
            raise ConfigStoreError("config_invalid", "Configuration files must be UTF-8.") from exc

        default = _safe_config_value(config_dict.get("default", {}))
        providers = []
        for provider in PROVIDERS:
            key_names = PROVIDER_KEY_ENV.get(provider, ())
            key_value = next(
                (
                    os.getenv(key) if key in SERVICE_ENVIRONMENT_KEYS else env_values.get(key)
                    for key in key_names
                    if (os.getenv(key) if key in SERVICE_ENVIRONMENT_KEYS else env_values.get(key))
                ),
                None,
            )
            base_key = PROVIDER_BASE_URL_ENV.get(provider)
            base_url = (
                (
                    os.getenv(base_key)
                    if base_key in SERVICE_ENVIRONMENT_KEYS
                    else env_values.get(base_key)
                )
                if base_key
                else None
            )
            providers.append(
                {
                    "name": provider,
                    "configured": bool(key_value),
                    "key_preview": _mask(key_value),
                    "key_source": _source_for(key_names[0], env_values)
                    if key_names
                    else "not configured",
                    "base_url": _safe_base_url(base_url),
                    "base_url_source": _source_for(base_key, env_values)
                    if base_key
                    else "not applicable",
                }
            )
        return {
            "version": _sha(config_bytes, env_bytes),
            "default": default,
            "providers": providers,
            "sources": {"default": "artemis.jsonc", "env": ".env"},
        }

    async def read(self) -> dict[str, Any]:
        async with self._async_lock:
            return await asyncio.to_thread(self._read_snapshot_with_lock)

    def _read_snapshot_with_lock(self) -> dict[str, Any]:
        with self._file_lock():
            return self._read_snapshot_locked()

    async def save(
        self,
        *,
        expected_version: str,
        default: dict[str, Any],
        credentials: dict[str, str | None],
        base_urls: dict[str, str | None],
        actor_email: str | None,
    ) -> dict[str, Any]:
        async with self._async_lock:
            return await asyncio.to_thread(
                self._save_locked,
                expected_version,
                default,
                credentials,
                base_urls,
                actor_email,
            )

    def _save_locked(
        self,
        expected_version: str,
        default: dict[str, Any],
        credentials: dict[str, str | None],
        base_urls: dict[str, str | None],
        actor_email: str | None,
    ) -> dict[str, Any]:
        with self._file_lock():
            old_config, old_env = self._read_bytes()
            checkout_root = ROOT_DIR.resolve()
            if self.config_path.is_relative_to(checkout_root) or self.env_path.is_relative_to(
                checkout_root
            ):
                raise ConfigStoreError(
                    "config_unwritable",
                    "Writable artemis.jsonc and .env files must be outside the checkout; check ARTEMIS_ARTEMIS_JSONC.",
                    409,
                )
            current_version = _sha(old_config, old_env)
            if expected_version != current_version:
                raise ConfigStoreError(
                    "config_conflict", "Settings changed elsewhere. Reload to review.", 409
                )
            config_text = old_config.decode("utf-8")
            config_dict = _validate_config_text(config_text)
            _validate_config_text(_replace_default(config_text, default))

            env_updates: dict[str, str | None] = {}
            for provider, value in credentials.items():
                if provider not in PROVIDER_KEY_ENV:
                    raise ConfigStoreError("config_invalid", f"Unsupported provider: {provider}.")
                for key in PROVIDER_KEY_ENV[provider]:
                    if key in SERVICE_ENVIRONMENT_KEYS:
                        raise ConfigStoreError(
                            "config_source_conflict",
                            f"{key} is set by the service environment and is read-only.",
                            409,
                        )
                    env_updates[key] = value.strip() if value and value.strip() else None

            audit_changes = [f"credential:{provider}" for provider in credentials]
            old_env_values = {
                key: value
                for key, value in dotenv_values(stream=io.StringIO(old_env.decode("utf-8"))).items()
            }
            for provider, value in base_urls.items():
                env_key = PROVIDER_BASE_URL_ENV.get(provider)
                if not env_key:
                    raise ConfigStoreError(
                        "config_invalid", f"Base URL is not supported for {provider}."
                    )
                if env_key in SERVICE_ENVIRONMENT_KEYS:
                    raise ConfigStoreError(
                        "config_source_conflict",
                        f"{env_key} is set by the service environment and is read-only.",
                        409,
                    )
                _validate_base_url(provider, value)
                env_updates[env_key] = value.strip() if value and value.strip() else None
                audit_changes.append(f"base_url:{provider}")

            new_config = _replace_default(config_text, default).encode("utf-8")
            new_env = _set_dotenv_values(old_env, env_updates)
            old_version = current_version
            new_version = _sha(new_config, new_env)
            audit_record = {
                "event": "admin_config_changed",
                "actor": actor_email,
                "changed": audit_changes + ["default_model"],
                "old_version": old_version,
                "new_version": new_version,
                "base_url_hosts": {
                    provider: {
                        "before": urlsplit(old_env_values.get(env_key) or "").hostname,
                        "after": urlsplit(base_urls.get(provider) or "").hostname,
                    }
                    for provider, env_key in PROVIDER_BASE_URL_ENV.items()
                    if provider in base_urls
                },
            }
            try:
                if self.env_path.exists():
                    _atomic_write(Path(f"{self.env_path}.bak"), old_env, 0o600)
                if self.config_path.exists():
                    _atomic_write(Path(f"{self.config_path}.bak"), old_config)
                _atomic_write(self.env_path, new_env, 0o600)
                _atomic_write(self.config_path, new_config)
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                audit_fd = os.open(
                    self.audit_path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(audit_fd, (json.dumps(audit_record, sort_keys=True) + "\n").encode())
                    os.fsync(audit_fd)
                finally:
                    os.close(audit_fd)
            except OSError as exc:
                try:
                    if old_env:
                        _atomic_write(self.env_path, old_env, 0o600)
                    else:
                        self.env_path.unlink(missing_ok=True)
                    _atomic_write(self.config_path, old_config)
                except OSError:
                    logger.exception("Config rollback failed")
                raise ConfigStoreError(
                    "config_unwritable",
                    "Settings could not be saved; previous files were restored.",
                    409,
                ) from exc

            for provider, value in credentials.items():
                settings.set_api_key(provider, value or "", persist_to_env=False)
            for provider, value in base_urls.items():
                env_key = PROVIDER_BASE_URL_ENV[provider]
                if value and value.strip():
                    os.environ[env_key] = value.strip()
                else:
                    os.environ.pop(env_key, None)
            return self._read_snapshot_locked()

    async def snapshot_for_spawn(self) -> SpawnConfigSnapshot:
        async with self._async_lock:
            return await asyncio.to_thread(self._snapshot_for_spawn_locked)

    def _snapshot_for_spawn_locked(self) -> SpawnConfigSnapshot:
        with self._file_lock():
            config_bytes, env_bytes = self._read_bytes()
            env_values = dotenv_values(stream=io.StringIO(env_bytes.decode("utf-8")))
            environment = os.environ.copy()
            managed_keys = {key for keys in PROVIDER_KEY_ENV.values() for key in keys} | set(
                PROVIDER_BASE_URL_ENV.values()
            )
            for key in managed_keys - SERVICE_ENVIRONMENT_KEYS:
                environment.pop(key, None)
            for key, value in env_values.items():
                if value is not None and key not in SERVICE_ENVIRONMENT_KEYS:
                    environment[key] = value
            fd, name = tempfile.mkstemp(prefix="artemis-run-config-", suffix=".jsonc")
            try:
                with os.fdopen(fd, "wb") as stream:
                    if hasattr(os, "fchmod"):
                        os.fchmod(stream.fileno(), 0o600)
                    else:
                        os.chmod(name, 0o600)
                    stream.write(config_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                Path(name).unlink(missing_ok=True)
                raise
            config_path = Path(name)
            environment["ARTEMIS_ARTEMIS_JSONC"] = str(config_path)
            return SpawnConfigSnapshot(environment, config_path)

    @staticmethod
    def cleanup_snapshot(snapshot: SpawnConfigSnapshot | None) -> None:
        if snapshot is not None:
            snapshot.config_path.unlink(missing_ok=True)


def get_config_store() -> ConfigStore:
    return ConfigStore.from_environment()
