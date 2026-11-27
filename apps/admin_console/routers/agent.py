"""Machine routes for host agents (CHE-1095). Every route authenticates in the app."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
import re
import threading
from sys import float_info
import time
from typing import Any, NoReturn
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Request, Response, WebSocket
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
from apps.admin_console.services import recording_uploads as ru
from apps.admin_console.services import run_storage
from apps.admin_console.services.host_hub import CLOSE_REVOKED, host_hub
from apps.admin_console.services.host_registry import RegistryError, host_registry
from apps.admin_console.services.host_tunnel import host_tunnels
from apps.admin_console.services.host_admission import host_admission
from apps.admin_console.services.recording_assignments import RecordingAssignmentStore
from artemis.config.paths import get_app_dir
from artemis.runtime.host_protocol import CONTRACT, Frame, ProtocolError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

HELLO_TIMEOUT_SECONDS = 10
DEAD_AFTER_SECONDS = CONTRACT.dead_seconds
MIN_WAIT_SECONDS = 0.05
MAX_RECORDING_SNAPSHOT_ROWS = 64


def _audience(headers: Any) -> str:
    return headers.get("host", "").strip().casefold()


def _json_message(message: Mapping[str, Any]) -> Any:
    payload = message.get("text")
    if not isinstance(payload, str):
        raise ProtocolError("Invalid host message")
    try:
        if len(payload.encode("utf-8")) > CONTRACT.max_frame:
            raise ProtocolError("Invalid host message")
    except UnicodeError as error:
        raise ProtocolError("Invalid host UTF-8") from error
    return json.loads(payload)


def _recording_store() -> RecordingAssignmentStore:
    return RecordingAssignmentStore(host_registry.db_path, clock=host_registry.clock)


def _handle_recording_ack(
    store: RecordingAssignmentStore, host_id: str, message: dict[str, Any]
) -> None:
    recording_id = message.get("recording_id")
    revision = message.get("revision")
    if not isinstance(recording_id, str) or len(recording_id) > 64:
        reason = "invalid_recording_id"
    elif type(revision) is not int or revision < 1:
        reason = "invalid_revision"
    else:
        if store.acknowledge(host_id, recording_id, revision):
            logger.info(
                "event=recording_acked host_id=%s recording_id=%s revision=%d",
                host_id,
                recording_id,
                revision,
            )
        return
    logger.info("event=recording_ack_ignored host_id=%s reason=%s", host_id, reason)


def _handle_recorder_ready(
    store: RecordingAssignmentStore, host_id: str, message: dict[str, Any], clock_offset: float
) -> None:
    recording_id = message.get("recording_id")
    revision = message.get("revision")
    started_at = message.get("started_at")
    if not isinstance(recording_id, str) or len(recording_id) > 64:
        reason = "invalid_recording_id"
    elif type(revision) is not int or revision < 1:
        reason = "invalid_revision"
    elif (
        type(started_at) not in (int, float) or not -float_info.max <= started_at <= float_info.max
    ):
        reason = "invalid_started_at"
    elif store.mark_ready(host_id, recording_id, revision, float(started_at), clock_offset):
        logger.info(
            "event=recorder_ready host_id=%s recording_id=%s revision=%d",
            host_id,
            recording_id,
            revision,
        )
        return
    else:
        reason = "not_matched"
    logger.info("event=recorder_ready_ignored host_id=%s reason=%s", host_id, reason)


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


def _artifact_unavailable() -> NoReturn:
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


@router.post("/unenroll")
async def unenroll(host: dict = Depends(require_agent_token("connect"))) -> dict:
    host_id = host["id"]
    host_registry.revoke(host_id)
    await host_tunnels.abort_host(host_id, "auth_expired")
    await host_hub.close_host(host_id, CLOSE_REVOKED, "revoked")
    logger.info("event=host_unenrolled host_id=%s", host_id)
    return {"status": "revoked"}


_MAX_CREATE_BODY_BYTES = 4096
_CHECKSUM_HEADER = re.compile(r"sha256 ([0-9a-f]{64})")


def _staging_root() -> Path:
    """Private staging area: a sibling of ``traces/`` under the app dir (ARTEMIS_APP_DIR)."""
    return get_app_dir() / "recording-uploads"


_upload_store_guard = threading.Lock()
_upload_store_instance: ru.RecordingUploadStore | None = None


def _upload_store() -> ru.RecordingUploadStore:
    # One instance per process: its lock and the per-host upload limit cover every request.
    # Construction creates and recovers the staging dir (blocking IO); call it off the loop.
    global _upload_store_instance
    with _upload_store_guard:
        if _upload_store_instance is None:
            _upload_store_instance = ru.RecordingUploadStore(_staging_root())
        return _upload_store_instance


def _reset_upload_store() -> None:
    global _upload_store_instance
    with _upload_store_guard:
        _upload_store_instance = None


_LEGACY_RECORDING_ID = re.compile(r"[0-9a-f]{32}")


def _store_recording_id(recording_id: str) -> str:
    """Rows written before the dashed-UUID fix hold 32-hex ids; the store takes dashed UUIDs."""
    if _LEGACY_RECORDING_ID.fullmatch(recording_id):
        return str(UUID(recording_id))
    return recording_id


_UPLOAD_ERRORS: dict[type[Exception], tuple[int, str]] = {
    ru.InvalidUpload: (400, "upload_invalid"),
    ru.UploadNotFound: (404, "upload_not_found"),
    ru.UploadConflict: (409, "upload_conflict"),
    ru.DeclaredLengthExceeded: (409, "length_exceeded"),
    ru.ChunkTooLarge: (413, "chunk_too_large"),
    ru.ChecksumMismatch: (422, "checksum_mismatch"),
    ru.TooManyUploads: (429, "too_many_uploads"),
    ru.InsufficientStorage: (507, "insufficient_storage"),
}


def _upload_error(
    status: int, code: str, detail: str, headers: dict[str, str] | None = None
) -> AdminAPIError:
    return AdminAPIError(
        status, detail, code, "Retry from the offset the server reports.", headers=headers
    )


def _upload_failure(error: Exception) -> AdminAPIError:
    if isinstance(error, ru.OffsetConflict):
        return _upload_error(
            409,
            "offset_conflict",
            "The chunk does not start at the stored offset.",
            {"Upload-Offset": str(error.durable_offset)},
        )
    if isinstance(error, ru.InsufficientStorage):
        run_storage.note_insufficient_storage(host_registry.db_path)
    status, code = _UPLOAD_ERRORS[type(error)]
    return _upload_error(status, code, code.replace("_", " "))


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the body as a stream; refuse once it passes ``limit`` bytes, never buffering more."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise ru.ChunkTooLarge
    parts: list[bytes] = []
    size = 0
    async for part in request.stream():
        size += len(part)
        if size > limit:
            raise ru.ChunkTooLarge
        parts.append(part)
    return b"".join(parts)


async def _upload_call(host_id: str, method: str, *args: Any) -> Any:
    """Run a blocking store call off the event loop and map its failures to API errors."""
    try:
        store = await anyio.to_thread.run_sync(_upload_store)
        return await anyio.to_thread.run_sync(getattr(store, method), host_id, *args)
    except (*_UPLOAD_ERRORS, ru.OffsetConflict) as error:
        raise _upload_failure(error) from error


@router.post("/uploads")
async def create_upload(
    request: Request, host: dict = Depends(require_agent_token("uploads"))
) -> dict:
    try:
        body = json.loads(await _read_capped(request, _MAX_CREATE_BODY_BYTES))
    except (ru.ChunkTooLarge, ValueError):
        body = None
    fields = (
        [body.get(k) for k in ("session_id", "recording_id", "length", "sha256")]
        if isinstance(body, dict)
        else []
    )
    if len(fields) != 4 or not all(isinstance(fields[i], str) for i in (0, 1, 3)):
        raise _upload_failure(ru.InvalidUpload())
    session_id, recording_id, length, digest = fields
    # A host may upload only for a recording the server assigned to it for this session.
    assigned = await anyio.to_thread.run_sync(
        _recording_store().is_assigned, host["id"], recording_id, session_id
    )
    if not assigned:
        raise _upload_error(404, "recording_unknown", "recording unknown")
    upload_id, offset = await _upload_call(
        host["id"], "create", session_id, _store_recording_id(recording_id), length, digest
    )
    return {"upload_id": upload_id, "offset": offset}


@router.get("/uploads/{upload_id}")
async def upload_offset(
    upload_id: str, host: dict = Depends(require_agent_token("uploads"))
) -> dict:
    return {"offset": await _upload_call(host["id"], "offset", upload_id)}


@router.patch("/uploads/{upload_id}")
async def append_upload(
    upload_id: str, request: Request, host: dict = Depends(require_agent_token("uploads"))
) -> dict:
    offset = request.headers.get("upload-offset", "")
    checksum = _CHECKSUM_HEADER.fullmatch(request.headers.get("upload-checksum", ""))
    if not (offset.isascii() and offset.isdigit() and checksum):
        raise _upload_failure(ru.InvalidUpload())
    try:
        chunk = await _read_capped(request, ru.MAX_CHUNK_BYTES)
    except ru.ChunkTooLarge as error:
        raise _upload_failure(error) from error
    new_offset = await _upload_call(
        host["id"], "append", upload_id, int(offset), chunk, checksum[1]
    )
    return {"offset": new_offset}


@router.delete("/uploads/{upload_id}", status_code=204)
async def abort_upload(
    upload_id: str, host: dict = Depends(require_agent_token("uploads"))
) -> Response:
    await _upload_call(host["id"], "abort", upload_id)
    return Response(status_code=204)


@router.websocket("/connect", dependencies=[Depends(public_tier), Depends(require_agent_websocket)])
async def connect(ws: WebSocket) -> None:
    await ws.accept()
    session: dict[str, Any] | None = None
    host_id = ""
    tunnel = None
    reason = "disconnected"
    try:
        try:
            frame = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_SECONDS)
            received_at = host_registry.clock()
            hello = _json_message(frame)
            host = host_registry.authenticate_hello(hello, _audience(ws.headers))
            clock_offset = received_at - int(hello["timestamp"])
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
        recording_store = _recording_store()
        recordings = recording_store.reconcile(host_id, host_registry.clock())
        connected = {
            "type": "connected",
            "token": session["token"],
            "expires_at": session["expires_at"],
            "generation": generation,
            "protocol_version": hr.PROTOCOL_VERSION,
            "min_supported": hr.MIN_SUPPORTED,
            "leases": host_tunnels.leased_devices(host_id),
            "recordings": [
                {
                    "recording_id": assignment.recording_id,
                    "session_id": assignment.session_id,
                    "run_generation": assignment.run_generation,
                    "desired_state": assignment.desired_state,
                    "revision": assignment.revision,
                    "deadline": assignment.deadline,
                }
                for assignment in recordings[:MAX_RECORDING_SNAPSHOT_ROWS]
            ],
        }
        # From here, lease changes are sent as they happen; nothing can precede "connected".
        tunnel.ready = True
        await tunnel.send_json(connected)
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
                    reported = message.get("devices")
                    emulators = {
                        str(item.get("serial"))
                        for item in (reported if isinstance(reported, list) else [])
                        if isinstance(item, dict) and item.get("kind") == "emulator"
                    }
                    for serial in previous_shared - current_shared:
                        host_admission.unshare_device(host_id, serial)
                    for serial in current_shared:
                        host_admission.share_device(host_id, serial, emulator=serial in emulators)
                    tunnel.unshare()
            elif kind == "event":
                fields = host_registry.audit_event(message)
                if fields is not None:  # never echo an unvalidated value into the log
                    logger.info("event=%s host_id=%s%s", message["event"], host_id, fields)
            elif kind == "recording_ack":
                _handle_recording_ack(recording_store, host_id, message)
            elif kind == "recorder_ready":
                _handle_recorder_ready(recording_store, host_id, message, clock_offset)
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
