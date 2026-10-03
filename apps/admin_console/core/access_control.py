from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any

import httpx
import jwt
from fastapi import Depends, Request, WebSocketException
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from jwt.exceptions import InvalidKeyError, PyJWTError
from starlette.requests import HTTPConnection
from starlette.status import WS_1008_POLICY_VIOLATION

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessConfig:
    auth_mode: str = "open"
    audience: str | None = None
    issuer: str | None = None
    admin_emails: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AccessIdentity:
    email: str | None
    admin: bool
    auth_mode: str
    reason: str | None = None


class AdminAPIError(Exception):
    def __init__(self, status_code: int, detail: str, code: str, fix: str):
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.fix = fix


def admin_api_error_handler(_request: Request, exc: AdminAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code, "fix": exc.fix},
    )


def config_from_environment() -> AccessConfig:
    auth_mode = os.getenv("ARTEMIS_AUTH_MODE", "open").strip().casefold()
    if auth_mode not in {"open", "cloudflare"}:
        raise ValueError("ARTEMIS_AUTH_MODE must be 'open' or 'cloudflare'.")

    if auth_mode == "open":
        return AccessConfig(auth_mode="open")

    audience = os.getenv("ARTEMIS_CF_ACCESS_AUD", "").strip()
    team_domain = os.getenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "").strip().casefold()
    if not audience:
        raise ValueError("ARTEMIS_CF_ACCESS_AUD is required when ARTEMIS_AUTH_MODE=cloudflare.")
    if not team_domain:
        raise ValueError(
            "ARTEMIS_CF_ACCESS_TEAM_DOMAIN is required when ARTEMIS_AUTH_MODE=cloudflare."
        )

    team_domain = re.sub(r"^https?://", "", team_domain).rstrip("/")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", team_domain):
        raise ValueError("ARTEMIS_CF_ACCESS_TEAM_DOMAIN must be a Cloudflare Access team domain.")
    if not team_domain.endswith(".cloudflareaccess.com"):
        team_domain = f"{team_domain}.cloudflareaccess.com"

    admin_emails = frozenset(
        email.strip().casefold()
        for email in os.getenv("ARTEMIS_ADMIN_EMAILS", "congvc.dev@gmail.com").split(",")
        if email.strip()
    )
    return AccessConfig(
        auth_mode="cloudflare",
        audience=audience,
        issuer=f"https://{team_domain}",
        admin_emails=admin_emails,
    )


class CloudflareAccessVerifier:
    def __init__(self, fetch_jwks=None, ttl_seconds: float = 3600.0):
        self._fetch_jwks = fetch_jwks or self._http_fetch_jwks
        self._ttl_seconds = ttl_seconds
        self._jwks: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._last_unknown_refresh = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    async def _http_fetch_jwks(url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
            raise ValueError("Cloudflare Access returned an invalid JWKS document.")
        return body

    async def _load_jwks(self, url: str, refresh_unknown: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        async with self._lock:
            if self._jwks is not None and now < self._expires_at:
                if not refresh_unknown or now - self._last_unknown_refresh < 60.0:
                    return self._jwks
            result = self._fetch_jwks(url)
            if asyncio.iscoroutine(result):
                result = await result
            if not isinstance(result, dict) or not isinstance(result.get("keys"), list):
                raise ValueError("Cloudflare Access returned an invalid JWKS document.")
            self._jwks = result
            self._expires_at = now + self._ttl_seconds
            if refresh_unknown:
                self._last_unknown_refresh = now
            return result

    async def verify(self, token: str, config: AccessConfig) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            raise InvalidTokenError("JWT signing key id is missing.")

        jwks_url = f"{config.issuer}/cdn-cgi/access/certs"
        jwks = await self._load_jwks(jwks_url)
        key = next((item for item in jwks["keys"] if item.get("kid") == key_id), None)
        if key is None:
            jwks = await self._load_jwks(jwks_url, refresh_unknown=True)
            key = next((item for item in jwks["keys"] if item.get("kid") == key_id), None)
        if key is None:
            raise InvalidTokenError("JWT signing key is unknown.")

        try:
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError("JWT signing key is invalid.") from exc
        return jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=config.audience,
            issuer=config.issuer,
            options={
                "require": ["aud", "email", "exp", "iat", "iss", "nbf", "sub"],
            },
            leeway=30,
        )


def _is_loopback_request(request: HTTPConnection) -> bool:
    peer = request.scope.get("artemis.transport_peer") or request.scope.get("client")
    host = peer[0] if isinstance(peer, (tuple, list)) and peer else None
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return isinstance(host, str) and host.casefold() == "localhost"


async def authenticate_request(
    request: HTTPConnection,
    config: AccessConfig,
    verifier: CloudflareAccessVerifier,
) -> AccessIdentity:
    if config.auth_mode == "open":
        forwarded = any(
            name.lower() == b"x-forwarded-for" for name, _value in request.scope.get("headers", ())
        )
        local_admin = _is_loopback_request(request) and not forwarded
        return AccessIdentity(
            email=None,
            admin=local_admin,
            auth_mode="open",
            reason=None if local_admin else "no_jwt",
        )

    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        return AccessIdentity(None, False, config.auth_mode, "no_jwt")

    try:
        claims = await verifier.verify(token, config)
    except (httpx.HTTPError, OSError, ValueError):
        logger.warning("Cloudflare Access JWKS unavailable")
        return AccessIdentity(None, False, config.auth_mode, "jwks_unavailable")
    except (InvalidTokenError, InvalidKeyError, PyJWTError, TypeError, KeyError):
        return AccessIdentity(None, False, config.auth_mode, "jwt_invalid")

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return AccessIdentity(None, False, config.auth_mode, "jwt_invalid")
    email = email.strip().casefold()
    return AccessIdentity(
        email,
        email in config.admin_emails,
        config.auth_mode,
        None if email in config.admin_emails else "not_on_allowlist",
    )


async def public_tier(request: HTTPConnection) -> AccessIdentity:
    config = getattr(request.app.state, "access_config", None) or config_from_environment()
    verifier = getattr(request.app.state, "access_verifier", None)
    if verifier is None:
        verifier = CloudflareAccessVerifier()
        request.app.state.access_verifier = verifier
    identity = await authenticate_request(request, config, verifier)
    request.state.identity = identity
    return identity


async def require_admin(identity: AccessIdentity = Depends(public_tier)) -> AccessIdentity:
    if identity.admin:
        return identity
    if identity.email is None:
        raise AdminAPIError(
            401,
            "Sign in through Cloudflare Access before using this admin action.",
            "not_signed_in",
            "Open the protected SmartQA URL and sign in, then retry.",
        )
    raise AdminAPIError(
        403,
        f"Admin access required for {identity.email}; add this address to ARTEMIS_ADMIN_EMAILS.",
        "admin_required",
        "Ask an administrator to add the signed-in address to ARTEMIS_ADMIN_EMAILS.",
    )


async def require_qa(identity: AccessIdentity = Depends(public_tier)) -> AccessIdentity:
    if identity.email is not None or identity.admin:
        return identity
    raise AdminAPIError(
        401,
        "Sign in through Cloudflare Access before using this QA action.",
        "not_signed_in",
        "Open the protected SmartQA URL and sign in, then retry.",
    )


async def require_websocket_admin(identity: AccessIdentity = Depends(public_tier)) -> None:
    if not identity.admin:
        reason = "not_signed_in" if identity.email is None else "admin_required"
        raise WebSocketException(code=WS_1008_POLICY_VIOLATION, reason=reason)


async def require_lifecycle_token(request: Request) -> None:
    expected = getattr(request.app.state, "lifecycle_token", None)
    supplied = request.headers.get("x-artemis-lifecycle-token")
    if not _is_loopback_request(request) or not (
        isinstance(expected, str)
        and isinstance(supplied, str)
        and hmac.compare_digest(expected, supplied)
    ):
        raise AdminAPIError(
            403,
            "Server lifecycle controls require the local lifecycle token.",
            "lifecycle_required",
            "Use the local Artemis CLI lifecycle command.",
        )


_PUBLIC_GET_PATHS = {
    "/api/system/readiness",
    "/api/system/adb/server",
    "/api/system/emulator/status",
    "/api/system/credentials",
    "/api/system/model-config-env",
    "/api/system/server-status",
    "/api/system/whoami",
    "/api/system/config",
    "/api/sessions",
    "/api/sessions/{session_id}",
    "/api/sessions/{session_id}/usage",
    "/api/sessions/{session_id}/tree",
    "/api/sessions/{session_id}/background_tasks",
    "/api/sessions/{session_id}/startup_progress",
    "/api/sessions/{session_id}/steps",
    "/api/steps/{step_id}/traces",
    "/api/traces/{trace_id}",
    "/api/traces/{trace_id}/download",
    "/api/stream/device-live",
    "/api/stream/device-state",
    "/api/tasks/presets",
    "/api/tasks/catalog",
    "/api/run/defaults",
    "/api/devices",
    "/api/status",
    "/api/stream",
    "/api/stream/{session_id}",
    "/admin",
    "/debug",
    "/images/{image_name}",
    "/api/images/{image_name}",
    "/videos/{video_path:path}",
    "/api/sessions/{session_id}/video",
    "/local_file",
    "/api/sessions/{session_id}/plan",
    "/api/sessions/{session_id}/notes",
    "/api/sessions/{session_id}/checks",
    "/api/replay/tools",
    "/api/replay/config",
    "/api/sessions/{session_id}/replay_steps",
    "/api/sessions/{session_id}/steps/{step_number}/replay_traces",
    "/",
    "/{full_path:path}",
}

_ADMIN_MUTATING_PATHS = {
    "/api/system/devices/select",
    "/api/system/adb/restart",
    "/api/system/adb/connect",
    "/api/system/adb/server/connect",
    "/api/system/adb/server/probe",
    "/api/system/adb/server/local",
    "/api/system/emulator/launch",
    "/api/system/emulator/stop",
    "/api/system/credentials/test",
    "/api/system/credentials",
    "/api/system/config",
    "/api/system/restart",
    "/api/cleanup",
    "/api/sessions/{session_id}/delete",
    "/api/sessions/{session_id}/steps/{step_number}/replay",
}

_QA_MUTATING_PATHS = {
    "/api/system/adb/heal-keys",
    "/api/system/emulator/dismiss",
}

_PUBLIC_MUTATING_PATHS = {
    "/api/run",
    "/api/stop",
    "/api/resume",
}


def route_tier(path: str, methods: set[str], is_websocket: bool = False) -> str | None:
    if is_websocket:
        return "public" if path == "/api/device-bridge/session" else None
    if path == "/api/system/shutdown" and methods == {"POST"}:
        return "lifecycle"
    if path == "/api/v1" or path.startswith("/api/v1/"):
        return "public"
    if methods == {"GET"} and path in _PUBLIC_GET_PATHS:
        return "public"
    if methods == {"POST"} and path in _PUBLIC_MUTATING_PATHS:
        return "public"
    if methods == {"POST"} and path in _QA_MUTATING_PATHS:
        return "qa"
    if methods in ({"POST"}, {"PUT"}) and path in _ADMIN_MUTATING_PATHS:
        return "admin"
    return None
