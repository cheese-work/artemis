"""Machine routes for host agents (CHE-1095). Every route authenticates in the app."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState

from apps.admin_console.core.access_control import AdminAPIError, public_tier
from apps.admin_console.core.agent_auth import (
    agent_error,
    require_agent_token,
    require_agent_websocket,
    require_challenge_attempt,
    require_enroll_attempt,
    require_enrollment_code,
)
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_hub import host_hub
from apps.admin_console.services.host_registry import RegistryError, host_registry

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

HELLO_TIMEOUT_SECONDS = 10
DEAD_AFTER_SECONDS = 50  # agents ping every 20 s


def _audience(headers: Any) -> str:
    return headers.get("host", "").strip().casefold()


class EnrollRequest(BaseModel):
    code: str = Field(max_length=128)
    public_key: str = Field(max_length=64)
    signature: str = Field(max_length=128)
    name: str = Field(default="", max_length=128)
    os: str = Field(default="", max_length=128)
    agent_version: str = Field(default="", max_length=64)
    protocol_version: int = 0


class ChallengeRequest(BaseModel):
    host_id: str = Field(max_length=64)


@router.get("/install.sh", dependencies=[Depends(require_enrollment_code)])
async def install_script() -> None:
    _artifact_unavailable()


@router.get("/dist/{artifact}", dependencies=[Depends(require_enrollment_code)])
async def download_artifact(artifact: str) -> None:
    _artifact_unavailable()


def _artifact_unavailable() -> None:
    # The installer and binaries ship with the host agent release (B3).
    raise AdminAPIError(
        404,
        "The installer is not published on this server yet.",
        "agent_artifact_unavailable",
        "Ask an administrator when the computer agent is available.",
    )


@router.post("/enroll", dependencies=[Depends(require_enroll_attempt)])
async def enroll(body: EnrollRequest) -> dict:
    try:
        result = host_registry.enroll(body.model_dump())
    except RegistryError as error:
        raise agent_error(error) from error
    logger.info("event=host_enrolled host_id=%s", result["host_id"])
    return result


@router.post("/challenge", dependencies=[Depends(require_challenge_attempt)])
async def challenge(body: ChallengeRequest, request: Request) -> dict:
    audience = _audience(request.headers)
    nonce = host_registry.issue_nonce(body.host_id, audience)
    if nonce is None:
        raise agent_error(RegistryError("rate_limited", 429))
    return {
        "nonce": nonce,
        "audience": audience,
        "expires_in": hr.NONCE_TTL_SECONDS,
        "protocol_version": hr.PROTOCOL_VERSION,
        "min_supported": hr.MIN_SUPPORTED,
    }


@router.post("/renew", dependencies=[Depends(require_agent_token("connect"))])
async def renew(request: Request) -> dict:
    expires_at = host_registry.renew(request.headers["authorization"][7:].strip())
    if expires_at is None:
        raise AdminAPIError(
            401, "The computer's session is not valid.", "token_invalid", "Reconnect the computer."
        )
    return {"expires_at": expires_at}


@router.websocket("/connect", dependencies=[Depends(public_tier), Depends(require_agent_websocket)])
async def connect(ws: WebSocket) -> None:
    await ws.accept()
    session: dict[str, Any] | None = None
    host_id = ""
    reason = "disconnected"
    try:
        try:
            hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_SECONDS)
            host = host_registry.authenticate_hello(hello, _audience(ws.headers))
            session = host_registry.begin_connection(host)
        except RegistryError as error:
            await ws.send_json({"type": "error", "code": error.code, **error.extra})
            await ws.close(code=error.status)
            return
        host_id = host["id"]
        generation = session["generation"]
        await host_hub.replace(host_id, ws, generation)
        logger.info("event=host_connected host_id=%s generation=%d", host_id, generation)
        await ws.send_json(
            {
                "type": "connected",
                "token": session["token"],
                "expires_at": session["expires_at"],
                "generation": generation,
                "protocol_version": hr.PROTOCOL_VERSION,
                "min_supported": hr.MIN_SUPPORTED,
            }
        )
        while True:
            # Idle sockets die at the token deadline too, not only when a frame arrives.
            wait = min(DEAD_AFTER_SECONDS, max(0.0, session["expires_at"] - host_registry.clock()))
            try:
                message = await asyncio.wait_for(ws.receive_json(), wait)
            except TimeoutError:
                if host_registry.clock() < session["expires_at"]:
                    raise
                message = {}
            # Every frame needs a live session: expired, revoked or superseded ends it.
            if host_registry.validate_token(session["token"], "connect") is None:
                reason = "auth_expired"
                if ws.application_state != WebSocketState.CONNECTED:
                    return  # revoked or superseded: the hub already closed this socket
                await ws.send_json({"type": "error", "code": "auth_expired"})
                await ws.close(code=4401)
                return
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "renew":
                expires_at = host_registry.renew(session["token"])
                if expires_at is None:
                    reason = "auth_expired"
                    await ws.send_json({"type": "error", "code": "auth_expired"})
                    await ws.close(code=4401)
                    return
                session["expires_at"] = expires_at
                await ws.send_json({"type": "renewed", "expires_at": expires_at})
            elif kind == "devices":
                host_registry.set_devices(host_id, generation, message.get("devices"))
    except TimeoutError:
        reason = "timeout"
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        if session is not None:
            host_hub.forget(host_id, session["generation"])
            host_registry.end_connection(host_id, session["generation"], reason)
            logger.info("event=host_lost host_id=%s reason=%s", host_id, reason)
