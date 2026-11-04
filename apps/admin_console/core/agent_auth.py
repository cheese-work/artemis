"""Operation-level guards for ``/api/agent/*`` (CHE-1095).

Cloudflare Access does not cover this prefix, so every machine route proves
itself here: an enrollment code, a key signature, or a session token. Each
guard carries ``agent_auth = True`` so the route-tier test can require one on
every route under the prefix.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Callable

from fastapi import WebSocketException
from starlette.requests import HTTPConnection

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_registry import host_registry


def _guard(fn: Callable) -> Callable:
    setattr(fn, "agent_auth", True)
    return fn


def agent_error(error: hr.RegistryError) -> AdminAPIError:
    fixes = {
        "code_invalid": "Check the code or create a new one from Setup → Computers.",
        "code_expired": "Create a new code from Setup → Computers.",
        "code_used": "Create a new code from Setup → Computers.",
        "rate_limited": "Wait a minute and try again.",
        "signature_invalid": "Re-run the install command on the computer.",
        "host_revoked": "This computer was removed. Connect it again with a new code.",
    }
    return AdminAPIError(
        error.status,
        error.code.replace("_", " "),
        error.code,
        fixes.get(error.code, "Retry, or create a new code from Setup → Computers."),
    )


def _is_loopback(host: str | None) -> bool:
    try:
        return bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


def client_ip(conn: HTTPConnection) -> str:
    """Peer address; ``CF-Connecting-IP`` counts only from the local cloudflared peer."""
    peer = conn.scope.get("artemis.transport_peer") or conn.scope.get("client")
    host = peer[0] if peer else None
    forwarded = conn.headers.get("cf-connecting-ip")
    return forwarded.strip() if forwarded and _is_loopback(host) else (host or "unknown")


def _require_enabled() -> None:
    if not hr.host_agent_enabled():
        raise AdminAPIError(
            404,
            "Computers are turned off on this server.",
            "host_agent_disabled",
            "Ask an administrator to set ARTEMIS_HOST_AGENT=enabled.",
        )


def _rate_limit(conn: HTTPConnection, name: str, limit: tuple[int, int]) -> None:
    if not host_registry.allow(f"{name}:{client_ip(conn)}", *limit):
        raise agent_error(hr.RegistryError("rate_limited", 429))


@_guard
async def require_enroll_attempt(conn: HTTPConnection) -> None:
    _require_enabled()
    _rate_limit(conn, "enroll", hr.ENROLL_IP_LIMIT)


@_guard
async def require_challenge_attempt(conn: HTTPConnection) -> None:
    _require_enabled()
    _rate_limit(conn, "challenge", hr.CHALLENGE_IP_LIMIT)


@_guard
async def require_enrollment_code(conn: HTTPConnection) -> None:
    _require_enabled()
    _rate_limit(conn, "enroll", hr.ENROLL_IP_LIMIT)
    code = conn.headers.get("x-artemis-enrollment-code", "")
    if not host_registry.allow(
        f"code:{hr._sha256(code)}", hr.CODE_ATTEMPT_LIMIT, hr.CODE_TTL_SECONDS
    ):
        raise agent_error(hr.RegistryError("rate_limited", 429))
    if not code or not host_registry.code_is_valid(code):
        raise AdminAPIError(
            401,
            "A valid enrollment code is required.",
            "code_invalid",
            "Create a new code from Setup → Computers.",
        )


def require_agent_token(scope: str) -> Callable:
    """Dependency factory: a live, unrevoked token for this scope; yields its host row."""

    @_guard
    async def guard(conn: HTTPConnection) -> dict[str, Any]:
        _require_enabled()
        header = conn.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        host = host_registry.validate_token(token, scope) if token else None
        if host is None:
            raise AdminAPIError(
                401,
                "The computer's session is not valid.",
                "token_invalid",
                "Reconnect the computer.",
            )
        return host

    return guard


@_guard
async def require_agent_websocket(conn: HTTPConnection) -> None:
    if not hr.host_agent_enabled():
        raise WebSocketException(code=1008, reason="host_agent_disabled")
