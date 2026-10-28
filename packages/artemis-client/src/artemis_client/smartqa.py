# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Agent entry point for SmartQA: upload an APK, then per theme lease, run and release.

Device preparation (fresh install, ``pm clear``, ``cmd uimode``) and the scripted
route replay run server side; this module only sends the request
(``options.smartqa`` on ``/api/run``) and keeps the lease balanced.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from artemis_client.client import ArtemisClient
from artemis_client.errors import ApiError, ArtemisClientError, ConflictError, TaskRejectedError

THEMES = ("light", "dark")
PERSONAS = ("first-time-user",)
DEFAULT_MAX_APK_BYTES = 200 * 1024 * 1024
#: Spend cap per check (plan rev-21); a scripted route makes no model calls.
MAX_MODEL_CALLS = 80

_PERSONA_BRIEFS = {
    "first-time-user": (
        "You are a first-time user with no developer knowledge who opened this app from a "
        "tester invite. Find out what the app demonstrates and visit each screen listed below, "
        "using only the labels you can see."
    ),
}
_SECRET_PLACEHOLDER = re.compile(r"^\$\{secret:[A-Za-z0-9_]+\}$")
_SWIPES = ("left", "right", "up", "down")


class CheckError(ArtemisClientError):
    """A check was refused or could not proceed; ``code`` is stable for callers and CI."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Screen:
    id: str
    purpose: str


@dataclass(frozen=True)
class Manifest:
    package: str
    screens: tuple[Screen, ...]
    test_accounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteStep:
    action: str
    text: str | None = None
    id: str | None = None
    direction: str | None = None
    value: str | None = None
    secret: bool = False

    def to_payload(self) -> dict[str, Any]:
        raw = {
            "action": self.action,
            "text": self.text,
            "id": self.id,
            "direction": self.direction,
            "value": self.value,
            "secret": self.secret or None,
        }
        return {key: value for key, value in raw.items() if value is not None}


_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_UNSUPPORTED_START = "!&*|>[{%@`"


def _scalar(raw: str) -> str:
    """Plain or simply quoted scalar; any other YAML syntax raises so it can never be misread."""
    raw = re.sub(r"\s+#.*$", "", raw).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        body = raw[1:-1]
        if raw[0] in body or (raw[0] == '"' and "\\" in body):
            raise ValueError(f"unsupported escape in quoted scalar {raw!r}")
        return body
    if raw[:1] in _UNSUPPORTED_START or raw[:1] in "\"'":
        raise ValueError(f"unsupported YAML syntax {raw!r}; use a plain or quoted scalar")
    return raw


def _key(raw: str) -> str:
    name = raw.strip()
    if not _PLAIN_KEY.match(name):
        raise ValueError(f"unsupported YAML key {name!r}")
    return name


def _parse_yaml_subset(text: str) -> dict[str, Any]:
    """Top-level scalars plus lists of scalars or flat mappings; JSON is accepted as-is."""
    if text.lstrip().startswith("{"):
        return json.loads(text)
    result: dict[str, Any] = {}
    key: str | None = None
    item: dict[str, str] | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or line.strip() == "---":
            continue
        stripped = line.strip()
        if not line[0].isspace():
            name, _, rest = stripped.partition(":")
            key, item = _key(name), None
            result[key] = _scalar(rest) if rest.strip() else []
        elif key is not None and isinstance(result[key], list) and stripped.startswith("- "):
            body = stripped[2:]
            name, sep, rest = body.partition(":")
            if sep and not body.startswith(("'", '"')):
                item = {_key(name): _scalar(rest)}
                result[key].append(item)
            else:
                item = None
                result[key].append(_scalar(body))
        elif item is not None and ":" in stripped:
            name, _, rest = stripped.partition(":")
            item[_key(name)] = _scalar(rest)
        else:
            raise ValueError(f"unsupported YAML line {stripped!r}")
    return result


def load_manifest(path: Path | str) -> Manifest:
    """Read ``smartqa.yml``. A manifest that marks a production back end is refused."""
    try:
        data = _parse_yaml_subset(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError("manifest_invalid", f"cannot read {path}: {exc}") from exc
    if str(data.get("backend", "")).strip().lower() == "production":
        raise CheckError("prod_backend_refused", "smartqa.yml marks a production back end")
    package = data.get("package")
    screens = data.get("screens")
    if not isinstance(package, str) or not package or not isinstance(screens, list) or not screens:
        raise CheckError("manifest_invalid", "smartqa.yml needs `package` and a `screens` list")
    parsed = []
    for entry in screens:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise CheckError("manifest_invalid", "every screen needs an `id`")
        parsed.append(Screen(str(entry["id"]), str(entry.get("purpose", ""))))
    accounts = data.get("test_accounts") or []
    return Manifest(
        package,
        tuple(parsed),
        tuple(str(a["name"] if isinstance(a, dict) else a) for a in accounts),
    )


def _step(raw: Any, index: int) -> RouteStep:
    def bad(reason: str) -> CheckError:
        return CheckError("route_invalid", f"step {index}: {reason}")

    if not isinstance(raw, Mapping):
        raise bad("must be an object")
    action = raw.get("action")
    text, element_id = raw.get("text"), raw.get("id")
    step = RouteStep(
        str(action),
        text=text,
        id=element_id,
        direction=raw.get("direction"),
        value=raw.get("value"),
        secret=bool(raw.get("secret", False)),
    )
    if action == "back":
        return step
    if action == "swipe":
        if step.direction not in _SWIPES:
            raise bad(f"swipe needs `direction` in {', '.join(_SWIPES)}")
        return step
    if action not in {"tap", "type"}:
        raise bad(f"unknown action {action!r}")
    if not (text or element_id):
        raise bad(f"{action} needs `text` or `id`")
    if action == "type":
        if not isinstance(step.value, str):
            raise bad("type needs a string `value`")
        if step.secret and not _SECRET_PLACEHOLDER.match(step.value):
            raise CheckError(
                "route_secret_literal",
                f"step {index}: a secret step must use a ${{secret:NAME}} placeholder",
            )
    return step


def load_route(path: Path | str) -> tuple[RouteStep, ...]:
    """Parse a scripted route (``{"steps": [...]}``): tap, swipe, back, type."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError("route_invalid", f"cannot read {path}: {exc}") from exc
    steps = data.get("steps") if isinstance(data, Mapping) else None
    if not isinstance(steps, list) or not steps:
        raise CheckError("route_invalid", "route file needs a non-empty `steps` list")
    return tuple(_step(raw, index) for index, raw in enumerate(steps, start=1))


@dataclass(frozen=True)
class CheckOptions:
    apk: Path
    manifest: Path
    themes: tuple[str, ...] = THEMES
    persona: str = "first-time-user"
    pool: str | None = None
    device_id: str | None = None
    route: Path | None = None
    max_apk_bytes: int = DEFAULT_MAX_APK_BYTES
    lease_ttl: int = 1800
    run_timeout: float = 1800.0


@dataclass(frozen=True)
class ThemeResult:
    theme: str
    task_id: str
    status: str
    device_serial: str | None = None
    error: str | None = None
    lease_release_error: str | None = None


@dataclass(frozen=True)
class CheckResult:
    apk_sha256: str
    themes: tuple[ThemeResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return bool(self.themes) and all(t.status in {"completed", "success"} for t in self.themes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "apk_sha256": self.apk_sha256,
            "themes": [t.__dict__ for t in self.themes],
        }


def _validate(options: CheckOptions) -> None:
    unknown = [t for t in options.themes if t not in THEMES]
    if not options.themes or unknown:
        raise CheckError("usage", f"themes must be a non-empty subset of {', '.join(THEMES)}")
    if options.persona not in PERSONAS:
        raise CheckError("usage", f"persona must be one of {', '.join(PERSONAS)}")
    if options.pool and options.device_id:
        raise CheckError("usage", "use --pool or --device, not both")


async def _upload(client: ArtemisClient, options: CheckOptions) -> Mapping[str, Any]:
    size = options.apk.stat().st_size
    if size > options.max_apk_bytes:
        raise CheckError("apk_too_large", f"{size} bytes exceeds the {options.max_apk_bytes} cap")
    data = options.apk.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    stored = await client.upload_bytes(
        "/api/smartqa/apks",
        data,
        content_type="application/vnd.android.package-archive",
        headers={"X-Content-SHA256": sha},
    )
    if stored.get("sha256") != sha:
        raise CheckError("apk_sha_mismatch", "the server stored different bytes than were sent")
    return {**stored, "sha256": sha}


def _smartqa_options(options: CheckOptions, manifest: Manifest, theme: str, route: Any) -> dict:
    # One check may spend MAX_MODEL_CALLS in total, so each theme gets an equal share.
    calls = MAX_MODEL_CALLS // len(options.themes)
    block: dict[str, Any] = {
        "theme": theme,
        "fresh_install": True,
        "clear_data": True,
        "screens": [s.id for s in manifest.screens],
    }
    if route:
        block.update(mode="route", route=[s.to_payload() for s in route], max_model_calls=0)
    else:
        block.update(mode="persona", persona=options.persona, max_model_calls=calls)
    return {"smartqa": block}


def _goal(options: CheckOptions, manifest: Manifest, route: Any) -> str:
    if route:
        return f"Replay the scripted SmartQA route on {manifest.package}."
    screens = "\n".join(f"- {s.id}: {s.purpose}" for s in manifest.screens)
    return f"{_PERSONA_BRIEFS[options.persona]}\nDeclared screens:\n{screens}"


_SELF_ENDED = frozenset({"completed", "success", "failed"})  # Statuses only the worker writes.


async def _stop_best_effort(client: ArtemisClient, task_id: str) -> None:
    """Ask the host to stop a task. No reply proves the worker exited, so none is awaited."""
    try:
        await client.stop(task_id)
    except ArtemisClientError:
        pass  # The lease expires on its own.


def _proven_not_admitted(exc: BaseException) -> bool:
    """True only when the failure proves the host never admitted the task.

    A 5xx, a timeout or a dropped connection is ambiguous: the host may have
    admitted the client-generated task before the failure.
    """
    if isinstance(exc, (TaskRejectedError, ValueError)):
        return True
    return isinstance(exc, ApiError) and 400 <= exc.status_code < 500 and exc.status_code != 408


async def _check_theme(
    client: ArtemisClient,
    options: CheckOptions,
    manifest: Manifest,
    route: Any,
    stored: Mapping[str, Any],
    theme: str,
) -> ThemeResult:
    lease_body = {"ttl": options.lease_ttl, "purpose": f"smartqa-check:{theme}"}
    lease_body.update({"pool": options.pool} if options.pool else {})
    lease_body.update({"device_id": options.device_id} if options.device_id else {})
    try:
        lease = await client.request_json("POST", "/api/leases", lease_body)
    except ConflictError as exc:
        raise CheckError("device_busy", f"{exc.detail}: {exc.payload}") from exc
    release_error: str | None = None
    task_id = str(uuid.uuid4())  # Known before submit, so an unknown outcome can still be stopped.
    submitted = False
    fenced = True  # While True a task may still run; the lease then expires on its own.
    try:
        handle = await client.submit(
            _goal(options, manifest, route),
            profile="pro",
            device_serial=lease.get("device_id"),
            locked_app_package=manifest.package,
            app_path=stored.get("path"),
            options=_smartqa_options(options, manifest, theme, route),
            task_id=task_id,
        )
        submitted = True
        task_id = handle.task_id
        result = await client.wait_for_task(task_id, timeout=options.run_timeout)
        # `cancelled` is written before the worker exits, so only a self-ended task frees the device.
        fenced = result.status not in _SELF_ENDED
        if fenced:
            await _stop_best_effort(client, task_id)
    except BaseException as exc:  # Includes cancellation.
        fenced = not (not submitted and _proven_not_admitted(exc))
        if fenced:
            await _stop_best_effort(client, task_id)
        raise
    finally:
        if fenced:
            release_error = "lease kept until its TTL: worker exit not proven"
        else:
            try:
                await client.request_json("POST", f"/api/leases/{lease['lease_id']}/release")
            except ArtemisClientError as exc:  # The lease auto-expires; never mask the outcome.
                release_error = str(exc)
    return ThemeResult(
        theme, result.task_id, result.status, result.device_serial, result.error, release_error
    )


async def run_check(client: ArtemisClient, options: CheckOptions) -> CheckResult:
    """Run one check: validate inputs locally, upload once, then each theme in order."""
    _validate(options)
    manifest = load_manifest(options.manifest)
    route = load_route(options.route) if options.route else None
    stored = await _upload(client, options)
    results = [
        await _check_theme(client, options, manifest, route, stored, theme)
        for theme in options.themes
    ]
    return CheckResult(stored["sha256"], tuple(results))
