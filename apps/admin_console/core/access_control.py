from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, cast

import httpx
import jwt
from fastapi import Depends, Request, WebSocketException
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from jwt.exceptions import InvalidKeyError, PyJWTError
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1013_TRY_AGAIN_LATER
from starlette.types import ASGIApp, Receive, Scope, Send

from apps.admin_console.database.repositories.principal_repository import (
    PrincipalStoreNotReady,
    principal_repo,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServicePrincipal:
    name: str
    routes: frozenset[tuple[str, str]]  # (METHOD, exact path)


@dataclass(frozen=True)
class AccessConfig:
    auth_mode: str = "open"
    audience: str | None = None
    issuer: str | None = None
    admin_emails: frozenset[str] = frozenset()
    admin_subjects: frozenset[str] = frozenset()
    spaces_enabled: bool = False
    service_principals: dict[str, ServicePrincipal] = field(default_factory=dict)

    def is_admin(self, subject: str | None, email: str | None) -> bool:
        """Global admins are keyed by subject; the email list is legacy, spaces-off only."""
        if subject is not None and subject in self.admin_subjects:
            return True
        return not self.spaces_enabled and email is not None and email in self.admin_emails


@dataclass(frozen=True)
class AccessIdentity:
    email: str | None
    admin: bool
    auth_mode: str
    reason: str | None = None
    issuer: str | None = None
    subject: str | None = None
    principal_id: str | None = None
    # Emails whose legacy run history this identity may claim. None: spaces are off, so an
    # email match alone still proves ownership (the pre-spaces rule).
    history_emails: frozenset[str] | None = None
    spaces: bool = False
    service_principal: str | None = None
    service_routes: frozenset[tuple[str, str]] = frozenset()


RETRY_AFTER_SECONDS = 5


class AdminAPIError(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        code: str,
        fix: str,
        retry_after: int | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.fix = fix
        self.retry_after = retry_after


def _error_response(exc: AdminAPIError) -> JSONResponse:
    content = {"detail": exc.detail, "code": exc.code, "fix": exc.fix}
    headers = {}
    if exc.retry_after is not None:
        content["retryable"] = True
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


async def admin_api_error_handler(conn: HTTPConnection, exc: AdminAPIError) -> JSONResponse | None:
    """An HTTP error response; a WebSocket handshake refusal for a WebSocket scope.

    A retryable error is a 503 denial response when the server supports it, else
    close code 1013 (try again later). Anything else is close code 1008 (policy).
    """
    response = _error_response(exc)
    if conn.scope["type"] != "websocket":
        return response
    websocket = cast(WebSocket, conn)
    if exc.retry_after is None:
        await websocket.close(code=WS_1008_POLICY_VIOLATION, reason=exc.code)
    elif "websocket.http.response" in conn.scope.get("extensions", {}):
        await websocket.send_denial_response(response)
    else:
        await websocket.close(code=WS_1013_TRY_AGAIN_LATER, reason=exc.code)
    return None


def _csv(name: str) -> frozenset[str]:
    return frozenset(
        item.strip().casefold() for item in os.getenv(name, "").split(",") if item.strip()
    )


def config_from_environment() -> AccessConfig:
    auth_mode = os.getenv("ARTEMIS_AUTH_MODE", "open").strip().casefold()
    if auth_mode not in {"open", "cloudflare"}:
        raise ValueError("ARTEMIS_AUTH_MODE must be 'open' or 'cloudflare'.")

    spaces_enabled = os.getenv("ARTEMIS_SPACES_ENABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if auth_mode == "open":
        return AccessConfig(auth_mode="open", spaces_enabled=spaces_enabled)

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

    admin_subjects = frozenset(
        item.strip() for item in os.getenv("ARTEMIS_ADMIN_SUBJECTS", "").split(",") if item.strip()
    )
    if spaces_enabled and not admin_subjects:
        logger.warning("Spaces are enabled but ARTEMIS_ADMIN_SUBJECTS is empty: no global admin.")
    return AccessConfig(
        auth_mode="cloudflare",
        audience=audience,
        issuer=f"https://{team_domain}",
        admin_emails=_csv("ARTEMIS_ADMIN_EMAILS"),
        admin_subjects=admin_subjects,
        spaces_enabled=spaces_enabled,
        service_principals=_service_principals_from_environment(),
    )


def _service_principals_from_environment() -> dict[str, ServicePrincipal]:
    """Parse ``ARTEMIS_SERVICE_PRINCIPALS``: ``{client_id: {"name": str, "routes": ["GET /path"]}}``."""
    raw = os.getenv("ARTEMIS_SERVICE_PRINCIPALS", "").strip()
    if not raw:
        return {}
    problem = (
        "ARTEMIS_SERVICE_PRINCIPALS must be a JSON object mapping a client id to "
        '{"name": "<principal>", "routes": ["GET /api/path"]}.'
    )
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(problem) from exc
    principals: dict[str, ServicePrincipal] = {}
    if not isinstance(mapping, dict):
        raise ValueError(problem)
    for client_id, entry in mapping.items():
        name = entry.get("name") if isinstance(entry, dict) else None
        routes = entry.get("routes") if isinstance(entry, dict) else None
        if not (
            client_id.strip()
            and isinstance(name, str)
            and name.strip()
            and isinstance(routes, list)
            and routes
            and all(isinstance(route, str) and len(route.split()) == 2 for route in routes)
        ):
            raise ValueError(problem)
        principals[client_id.strip()] = ServicePrincipal(
            name.strip(),
            frozenset((m.upper(), path) for m, path in (route.split() for route in routes)),
        )
    return principals


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
                "require": ["aud", "exp", "iat", "iss", "sub"],
            },
            leeway=30,
        )


_FORWARDING_HEADERS = (b"x-forwarded-for", b"forwarded", b"x-real-ip", b"cf-connecting-ip")


def require_loopback_bind(config: AccessConfig, host: str) -> None:
    """Refuse to serve open mode with spaces on any address but loopback (wildcards included)."""
    if config.auth_mode != "open" or not config.spaces_enabled:
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == "localhost"
    if not loopback:
        raise ValueError(
            f"ARTEMIS_SPACES_ENABLED with ARTEMIS_AUTH_MODE=open only serves loopback; got {host!r}."
        )


def _is_loopback_request(request: HTTPConnection) -> bool:
    peer = request.scope.get("artemis.transport_peer") or request.scope.get("client")
    host = peer[0] if isinstance(peer, (tuple, list)) and peer else None
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return isinstance(host, str) and host.casefold() == "localhost"


def _is_service_token(claims: dict[str, Any]) -> bool:
    """A Cloudflare Access service token has no email; its client id is `common_name`."""
    return claims.get("email") is None and "common_name" in claims


def _service_identity(claims: dict[str, Any], config: AccessConfig) -> AccessIdentity | None:
    if not _is_service_token(claims):
        return None
    client_id = claims["common_name"]
    principal = config.service_principals.get(client_id) if isinstance(client_id, str) else None
    if principal is None:
        return AccessIdentity(None, False, config.auth_mode, "service_principal_unmapped")
    return AccessIdentity(
        None,
        False,
        config.auth_mode,
        service_principal=principal.name,
        service_routes=principal.routes,
    )


async def authenticate_request(
    request: HTTPConnection,
    config: AccessConfig,
    verifier: CloudflareAccessVerifier,
) -> AccessIdentity:
    if config.auth_mode == "open":
        forwarded = any(
            name.lower() in _FORWARDING_HEADERS for name, _value in request.scope.get("headers", ())
        )
        local_admin = _is_loopback_request(request) and not forwarded
        if config.spaces_enabled and not local_admin:
            raise AdminAPIError(
                403,
                "Open mode with spaces enabled only admits a direct local caller.",
                "open_mode_loopback_only",
                "Set ARTEMIS_AUTH_MODE=cloudflare, or call from the server host without a proxy.",
            )
        return AccessIdentity(
            email=None,
            admin=local_admin,
            auth_mode="open",
            reason=None if local_admin else "no_jwt",
            spaces=config.spaces_enabled,
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

    service_identity = _service_identity(claims, config)
    if service_identity is not None:
        return service_identity
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return AccessIdentity(None, False, config.auth_mode, "jwt_invalid")
    email = email.strip().casefold()
    subject, issuer = claims.get("sub"), config.issuer  # decode() already checked iss == issuer
    if not (isinstance(subject, str) and subject and issuer):
        return AccessIdentity(None, False, config.auth_mode, "jwt_invalid")
    principal = None
    if config.spaces_enabled:
        try:
            principal = await asyncio.to_thread(principal_repo.ensure_user, issuer, subject, email)
        except PrincipalStoreNotReady as exc:
            raise AdminAPIError(
                503,
                "The principal store is not ready.",
                "principal_store_not_ready",
                "Retry shortly; restart the console if it persists so the schema can be created.",
                RETRY_AFTER_SECONDS,
            ) from exc
    admin = config.is_admin(subject, email)
    return AccessIdentity(
        email,
        admin,
        config.auth_mode,
        None if admin else "not_on_allowlist",
        issuer,
        subject,
        principal.id if principal else None,
        principal.history_emails if principal else None,
        config.spaces_enabled,
    )


async def public_tier(request: HTTPConnection) -> AccessIdentity:
    from apps.admin_console.core.preview_identity import resolve_preview_identity

    identity = getattr(request.state, "identity", None)
    if isinstance(identity, AccessIdentity):
        return identity
    preview_identity = resolve_preview_identity(request)
    if preview_identity is not None:
        request.state.identity = preview_identity
        return preview_identity
    config, verifier = _access_context(request)
    identity = await authenticate_request(request, config, verifier)
    request.state.identity = identity
    _enforce_service_principal_routes(request, identity)
    return identity


def _access_context(request: HTTPConnection) -> tuple[AccessConfig, CloudflareAccessVerifier]:
    config = getattr(request.app.state, "access_config", None) or config_from_environment()
    verifier = getattr(request.app.state, "access_verifier", None)
    if verifier is None:
        verifier = CloudflareAccessVerifier()
        request.app.state.access_verifier = verifier
    return config, verifier


async def _enforce_service_boundary(request: HTTPConnection) -> None:
    """Resolve only a service-token identity before routing; human callers resolve per route."""
    from apps.admin_console.core.preview_identity import resolve_preview_identity

    preview_error = None
    try:
        if resolve_preview_identity(request) is not None:
            return
    except AdminAPIError as error:
        preview_error = error  # The route's own public_tier reports it, unless a service token.
    config, verifier = _access_context(request)
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if config.auth_mode != "cloudflare" or not token:
        return
    try:
        # Same payload bytes as the verified claims, so a human token is skipped here and
        # verified once, by its route.
        if not _is_service_token(jwt.decode(token, options={"verify_signature": False})):
            return
    except PyJWTError:
        return  # Not a JWT at all; the route's own public_tier reports it.
    try:
        claims = await verifier.verify(token, config)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        _deny_unverified_service_token(request, "jwks_unavailable")
        raise AdminAPIError(
            503,
            "Cloudflare Access keys are unavailable, so this service credential cannot be verified.",
            "service_token_unverifiable",
            "Retry shortly.",
            RETRY_AFTER_SECONDS,
        ) from exc
    except (InvalidTokenError, PyJWTError, TypeError, KeyError) as exc:
        raise _invalid_service_token(request, "jwt_invalid") from exc
    except Exception as exc:  # Any other verifier failure still fails closed.
        raise _invalid_service_token(request, "verification_error") from exc
    identity = _service_identity(claims, config)
    if identity is None:
        return
    request.state.identity = identity
    if preview_error is not None:
        logger.warning(
            "Service token denied: preview identity present principal=%s route=%s",
            identity.service_principal or "unmapped",
            _log_safe_route(request),
        )
        raise preview_error
    _enforce_service_principal_routes(request, identity)


def _deny_unverified_service_token(request: HTTPConnection, reason: str) -> None:
    logger.warning(
        "Service token denied: verification failed reason=%s route=%s",
        reason,
        _log_safe_route(request),
    )


def _invalid_service_token(request: HTTPConnection, reason: str) -> AdminAPIError:
    _deny_unverified_service_token(request, reason)
    return AdminAPIError(
        401,
        "This service credential is not a valid Cloudflare Access token.",
        "service_token_invalid",
        "Use a current Cloudflare Access service token for this application.",
    )


class ServicePrincipalMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            try:
                await _enforce_service_boundary(HTTPConnection(scope))
            except AdminAPIError as error:
                if scope["type"] == "websocket":
                    await admin_api_error_handler(WebSocket(scope, receive, send), error)
                else:
                    await _error_response(error)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _log_safe_path(request: HTTPConnection) -> str:
    """The routed path with control characters escaped, so it cannot split a log line."""
    return request.scope["path"].encode("unicode_escape").decode("ascii")


def _log_safe_route(request: HTTPConnection) -> str:
    return f"{request.scope.get('method', 'WEBSOCKET')} {_log_safe_path(request)}"


def _enforce_service_principal_routes(request: HTTPConnection, identity: AccessIdentity) -> None:
    """A service token reaches only the exact routes its principal maps; nothing else."""
    if identity.reason == "service_principal_unmapped":
        logger.warning("Service token denied: unmapped client id route=%s", _log_safe_path(request))
        allowed = False
    elif identity.service_principal is None:
        return
    else:
        method = request.scope.get("method", "WEBSOCKET")
        # The router matches scope["path"]; request.url.path drops tabs and newlines.
        allowed = (method, request.scope["path"]) in identity.service_routes
        logger.info(
            "Service principal request principal=%s route=%s %s allowed=%s",
            identity.service_principal,
            method,
            _log_safe_path(request),
            allowed,
        )
    if allowed:
        return
    raise AdminAPIError(
        403,
        "This service credential cannot call this route.",
        "service_route_denied",
        "Use a route listed for the principal in ARTEMIS_SERVICE_PRINCIPALS.",
    )


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


async def require_admin_or_service_read(
    identity: AccessIdentity = Depends(public_tier),
) -> AccessIdentity:
    """Admin, or a service principal whose exact route `public_tier` already allowed."""
    if identity.service_principal is not None:
        return identity
    return await require_admin(identity)


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


async def require_effective_loopback(request: Request) -> None:
    """Admit only a direct local caller: loopback peer, no proxy forwarding."""
    forwarded = any(
        name.lower() in _FORWARDING_HEADERS for name, _value in request.scope.get("headers", ())
    )
    if forwarded or not _is_loopback_request(request):
        raise AdminAPIError(
            403,
            "This endpoint is local-only.",
            "loopback_required",
            "Call this endpoint directly from the server host.",
        )


_PUBLIC_GET_PATHS = {
    "/api/system/readiness",
    "/api/system/adb/server",
    "/api/system/emulator/status",
    "/api/system/credentials",
    "/api/system/model-config-env",
    "/api/system/server-status",
    "/api/system/whoami",
    "/api/system/version",
    "/api/system/config",
    "/api/runs",
    "/api/runs/{session_id}",
    "/api/runs/{session_id}/bundle.zip",
    "/api/system/retention",
    "/api/system/storage",
    "/api/hosts",
    "/api/hosts/enrollment-codes/{code_id}",
    "/api/sessions",
    "/api/sessions/{session_id}",
    "/api/sessions/{session_id}/usage",
    "/api/sessions/{session_id}/goal-images/{index}",
    "/api/sessions/{session_id}/events",
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

_ADMIN_GET_PATHS: set[str] = set()

# Admin reads that a mapped service principal may also call (see require_admin_or_service_read).
_SERVICE_READ_GET_PATHS = {"/api/system/failures"}

_ADMIN_MUTATING_PATHS = {
    "/api/system/failures/collect",
    "/api/system/failures/digest",
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
    "/api/runs/{session_id}/delete",
    "/api/runs/clear",
    "/api/system/retention",
    "/api/system/retention/dry-run",
    "/api/system/retention/run",
    "/api/sessions/{session_id}/steps/{step_number}/replay",
    "/api/hosts/enrollment-codes",
    "/api/hosts/{host_id}/revoke",
    "/api/hosts/{host_id}/rename",
}

# Machine routes for host agents. Cloudflare Access does not cover this prefix;
# each route authenticates in the application (code, signature or token).
_AGENT_PATHS = {
    "/api/agent/install.sh": {"GET"},
    "/api/agent/dist/{artifact}": {"GET"},
    "/api/agent/enroll": {"POST"},
    "/api/agent/challenge": {"POST"},
    "/api/agent/renew": {"POST"},
    "/api/agent/unenroll": {"POST"},
}

# Owner-or-admin actions: the route guard needs a signed-in user; the handler
# then checks the caller owns the run (see core/ownership.py).
_QA_MUTATING_PATHS = {
    "/api/sessions/{session_id}/delete",
    "/api/system/adb/heal-keys",
    "/api/system/emulator/dismiss",
    "/api/runs/{session_id}/pin",
    "/api/runs/{session_id}/unpin",
}

_PUBLIC_MUTATING_PATHS = {
    "/api/run",
    "/api/stop",
    "/api/tasks/{session_id}/cancel-queued",
    "/api/resume",
}


def route_tier(path: str, methods: set[str], is_websocket: bool = False) -> str | None:
    if is_websocket:
        if path == "/api/agent/connect":
            return "agent"
        return "public" if path == "/api/device-bridge/session" else None
    if methods == _AGENT_PATHS.get(path):
        return "agent"
    if path == "/api/system/shutdown" and methods == {"POST"}:
        return "lifecycle"
    if path == "/api/system/drain" and methods in ({"GET"}, {"POST"}, {"DELETE"}):
        return "loopback"
    if path == "/api/system/service-readiness" and methods == {"GET"}:
        return "loopback"
    if path == "/api/v1" or path.startswith("/api/v1/"):
        return "public"
    if methods == {"GET"} and path in _PUBLIC_GET_PATHS:
        return "public"
    if methods == {"GET"} and path in _ADMIN_GET_PATHS:
        return "admin"
    if methods == {"GET"} and path in _SERVICE_READ_GET_PATHS:
        return "admin_or_service"
    if methods == {"POST"} and path in _PUBLIC_MUTATING_PATHS:
        return "public"
    if methods == {"POST"} and path in _QA_MUTATING_PATHS:
        return "qa"
    if methods in ({"POST"}, {"PUT"}) and path in _ADMIN_MUTATING_PATHS:
        return "admin"
    return None
