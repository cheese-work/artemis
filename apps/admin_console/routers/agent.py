"""Machine routes for host agents (CHE-1095). Every route authenticates in the app."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketState
from starlette.responses import FileResponse

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
from apps.admin_console.services.host_tunnel import host_tunnels
from apps.admin_console.services.host_admission import host_admission
from artemis.runtime.host_protocol import CONTRACT, Frame, ProtocolError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

HELLO_TIMEOUT_SECONDS = 10
DEAD_AFTER_SECONDS = CONTRACT.dead_seconds
MIN_WAIT_SECONDS = 0.05


def _audience(headers: Any) -> str:
    return headers.get("host", "").strip().casefold()


def _json_message(message: dict) -> Any:
    payload = message.get("text")
    if not isinstance(payload, str):
        raise ProtocolError("Invalid host message")
    try:
        if len(payload.encode("utf-8")) > CONTRACT.max_frame:
            raise ProtocolError("Invalid host message")
    except UnicodeError as error:
        raise ProtocolError("Invalid host UTF-8") from error
    return json.loads(payload)


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
async def install_script() -> FileResponse:
    return _release_artifact("install.sh")


@router.get("/dist/{artifact}", dependencies=[Depends(require_enrollment_code)])
async def download_artifact(artifact: str) -> FileResponse:
    return _release_artifact(artifact)


def _release_artifact(artifact: str) -> FileResponse:
    allowed = {
        "install.sh",
        "SHA256SUMS",
        "smartqa-host-linux-amd64",
        "smartqa-host-darwin-arm64",
        "smartqa-host-windows-amd64.exe",
    }
    directory = os.environ.get("ARTEMIS_AGENT_DIST_DIR", "")
    if not directory or artifact not in allowed:
        _artifact_unavailable()
    try:
        root = Path(directory).resolve(strict=True)
        candidate = root / artifact
        if candidate.is_symlink():
            _artifact_unavailable()
        path = candidate.resolve(strict=True)
        if path.parent != root or not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
            _artifact_unavailable()
    except OSError:
        _artifact_unavailable()
    media_type = (
        "text/plain" if artifact in {"install.sh", "SHA256SUMS"} else "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type, filename=artifact)


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
    tunnel = None
    reason = "disconnected"
    try:
        try:
            hello = _json_message(await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_SECONDS))
            host = host_registry.authenticate_hello(hello, _audience(ws.headers))
            session = host_registry.begin_connection(host)
        except RegistryError as error:
            await ws.send_json({"type": "error", "code": error.code, **error.extra})
            await ws.close(code=error.status)
            return
        host_id = host["id"]
        generation = session["generation"]

        def shared_serials():
            _, devices = host_registry.list_hosts()
            return {device["serial"] for device in devices if device.get("computer_id") == host_id}

        tunnel = await host_tunnels.attach(host_id, generation, ws, shared_serials)
        await host_hub.replace(host_id, ws, generation)
        logger.info("event=host_connected host_id=%s generation=%d", host_id, generation)
        await tunnel.send_json(
            {
                "type": "connected",
                "token": session["token"],
                "expires_at": session["expires_at"],
                "generation": generation,
                "protocol_version": hr.PROTOCOL_VERSION,
                "min_supported": hr.MIN_SUPPORTED,
            }
        )
        deadline = session["expires_at"]
        last_seen = time.monotonic()
        next_ping = last_seen + CONTRACT.ping_seconds
        while True:
            # Idle sockets die at the token deadline too, not only when a frame arrives.
            now = time.monotonic()
            wait = max(
                MIN_WAIT_SECONDS,
                min(
                    DEAD_AFTER_SECONDS - (now - last_seen),
                    next_ping - now,
                    deadline - host_registry.clock(),
                ),
            )
            timed_out = False
            message: Any = None
            try:
                message = await asyncio.wait_for(ws.receive(), wait)
            except TimeoutError:
                timed_out = True
            # Every wake-up needs a live session; the stored expiry is the one authority,
            # so an HTTP renewal moves this socket's deadline too.
            live = host_registry.validate_token(session["token"], "connect")
            if live is None:
                reason = "auth_expired"
                if ws.application_state != WebSocketState.CONNECTED:
                    return  # revoked or superseded: the hub already closed this socket
                await tunnel.send_json({"type": "error", "code": "auth_expired"})
                await ws.close(code=4401)
                return
            deadline = live["token_expires_at"]
            if timed_out:
                if time.monotonic() - last_seen >= DEAD_AFTER_SECONDS:
                    raise TimeoutError  # silent for the full dead interval
                if time.monotonic() >= next_ping:
                    await tunnel.send_json({"type": "ping"})
                    next_ping = time.monotonic() + CONTRACT.ping_seconds
                continue  # woke at an outdated deadline; recompute from the stored one
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            last_seen = time.monotonic()
            if message.get("bytes") is not None:
                tunnel.mux.receive(Frame.decode(message["bytes"]))
                continue
            message = _json_message(message)
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "ping":
                await tunnel.send_json({"type": "pong"})
            elif kind == "renew":
                expires_at = host_registry.renew(session["token"])
                if expires_at is None:
                    reason = "auth_expired"
                    await tunnel.send_json({"type": "error", "code": "auth_expired"})
                    await ws.close(code=4401)
                    return
                deadline = expires_at
                await tunnel.send_json({"type": "renewed", "expires_at": expires_at})
            elif kind == "devices":
                previous_shared = shared_serials()
                if host_registry.set_devices(host_id, generation, message.get("devices")):
                    current_shared = shared_serials()
                    for serial in previous_shared - current_shared:
                        host_admission.unshare_device(host_id, serial)
                    for serial in current_shared:
                        host_admission.share_device(host_id, serial)
                    tunnel.unshare()
    except (ProtocolError, json.JSONDecodeError):
        reason = "bad_frame"
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.send_json({"type": "error", "code": "bad_frame"})
            await ws.close(code=4400)
    except TimeoutError:
        reason = "timeout"
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.close(code=4408)
    except (WebSocketDisconnect, ValueError):
        pass
    finally:
        if session is not None:
            host_hub.forget(host_id, session["generation"])
            with anyio.CancelScope(shield=True):
                await host_tunnels.disconnect(host_id, session["generation"], reason)
            host_registry.end_connection(host_id, session["generation"], reason)
            logger.info("event=host_lost host_id=%s reason=%s", host_id, reason)
