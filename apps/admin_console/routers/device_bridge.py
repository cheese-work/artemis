# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Loopback WebSocket relay for whole ADB packets."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import ipaddress
import json
import logging
import struct

from fastapi import APIRouter, Depends, WebSocket, WebSocketException
from starlette.websockets import WebSocketDisconnect

from apps.admin_console.core.access_control import AccessIdentity, public_tier
from artemis.utils.redaction import redact_text


try:
    from admin_console.services.bridge_session_service import (
        MAX_ADB_PACKET_BYTES,
        BridgeSession,
        bridge_session_service,
    )
except ImportError:
    from apps.admin_console.services.bridge_session_service import (
        MAX_ADB_PACKET_BYTES,
        BridgeSession,
        bridge_session_service,
    )

router = APIRouter(prefix="/api/device-bridge", tags=["device-bridge"])

_CLOSE_FORBIDDEN_REMOTE = 4003
_CLOSE_SESSION_EXPIRED = 4008
_CLOSE_BRIDGE_ERROR = 1011
_ADB_HEADER_BYTES = 24
_MAX_PENDING_BYTES = MAX_ADB_PACKET_BYTES * 4
_WEBSOCKET_SEND_TIMEOUT_SECONDS = 2
_CLIENT_CLOSE_REASONS = frozenset(
    {
        "manual_disconnect",
        "service_destroyed",
        "page_unload",
        "connect_error",
        "socket_open_timeout",
        "attachment_timeout",
        "socket_error",
        "socket_closed",
        "protocol_error",
        "bridge_rejected",
        "usb_read_error",
        "usb_write_error",
        "usb_disconnected",
        "another_tab",
    }
)

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep


async def require_bridge_access(
    websocket: WebSocket,
    identity: AccessIdentity = Depends(public_tier),
) -> None:
    if websocket.headers.get("x-forwarded-for") and not identity.email:
        raise WebSocketException(code=1008, reason="not_signed_in")


def _client_is_loopback(websocket: WebSocket) -> bool:
    client = websocket.scope.get("artemis.transport_peer", websocket.client)
    if not client:
        return False
    host = getattr(client, "host", None) or client[0]
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _adb_packet(frame: bytes) -> bytes | None:
    if not frame:
        return None
    if len(frame) < _ADB_HEADER_BYTES or len(frame) > MAX_ADB_PACKET_BYTES:
        raise ValueError("invalid ADB packet size")
    payload_size = struct.unpack_from("<I", frame, 12)[0]
    if payload_size + _ADB_HEADER_BYTES != len(frame):
        raise ValueError("WebSocket frame must contain one complete ADB packet")
    return frame


async def _forward_tcp_packets(
    reader: asyncio.StreamReader,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
    session: BridgeSession,
) -> None:
    while True:
        header = await reader.readexactly(_ADB_HEADER_BYTES)
        payload_size = struct.unpack_from("<I", header, 12)[0]
        if payload_size + _ADB_HEADER_BYTES > MAX_ADB_PACKET_BYTES:
            raise ValueError("ADB packet exceeds relay limit")
        payload = await reader.readexactly(payload_size)
        await _send_bytes(websocket, header + payload, send_lock)
        if not await _renew_lease(session, websocket, send_lock):
            return


async def _send_bytes(websocket: WebSocket, message: bytes, send_lock: asyncio.Lock) -> None:
    async def send_locked() -> None:
        async with send_lock:
            await websocket.send_bytes(message)

    await asyncio.wait_for(send_locked(), timeout=_WEBSOCKET_SEND_TIMEOUT_SECONDS)


async def _send_json(
    websocket: WebSocket,
    message: dict[str, object],
    send_lock: asyncio.Lock,
) -> None:
    async def send_locked() -> None:
        async with send_lock:
            await websocket.send_json(message)

    await asyncio.wait_for(send_locked(), timeout=_WEBSOCKET_SEND_TIMEOUT_SECONDS)


def _client_has_closed(message: Mapping[str, object], session: BridgeSession) -> bool:
    if message["type"] == "websocket.disconnect":
        session.close_reason = session.close_reason or "client"
        code = message.get("code")
        session.websocket_close_code = code if isinstance(code, int) else None
        session.websocket_close_reason = str(message.get("reason", ""))
        return True
    text = message.get("text")
    if text == "close":
        session.close_reason = "client"
        return True
    if not isinstance(text, str):
        return False
    if len(text) > 2048:
        raise ValueError("client close diagnostic exceeds limit")
    report = json.loads(text)
    if not isinstance(report, dict) or report.get("type") != "client_close":
        raise ValueError("expected a client close diagnostic")
    reason = report.get("reason")
    usb_error = report.get("usb_error")
    visibility = report.get("visibility_state")
    if (
        not isinstance(reason, str)
        or reason not in _CLIENT_CLOSE_REASONS
        or (usb_error is not None and (not isinstance(usb_error, str) or len(usb_error) > 512))
        or visibility not in (None, "hidden", "visible")
    ):
        raise ValueError("invalid client close diagnostic")
    session.client_close = {
        "reason": reason,
        "usb_error": redact_text(usb_error) if usb_error else None,
        "visibility_state": visibility,
    }
    session.close_reason = "client"
    return True


async def _forward_websocket_packets(
    websocket: WebSocket,
    writer: asyncio.StreamWriter,
    receive_task: asyncio.Task[dict[str, object]],
    session: BridgeSession,
    send_lock: asyncio.Lock,
) -> None:
    while True:
        message = await receive_task
        if _client_has_closed(message, session):
            return
        frame = message.get("bytes")
        if frame is None:
            raise ValueError("expected a binary ADB packet or close message")
        packet = _adb_packet(frame)
        if packet is not None:
            writer.write(packet)
            await writer.drain()
            if not await _renew_lease(session, websocket, send_lock):
                return
        receive_task = asyncio.create_task(websocket.receive())


async def _relay_packets(
    websocket: WebSocket,
    session: BridgeSession,
    send_lock: asyncio.Lock,
) -> None:
    accept_task = asyncio.create_task(session.connected.wait())
    receive_task = asyncio.create_task(websocket.receive())
    tcp_to_websocket_task: asyncio.Task[None] | None = None
    websocket_to_tcp_task: asyncio.Task[None] | None = None
    pending_packets: list[bytes] = []
    pending_bytes = 0

    try:
        while not session.connected.is_set():
            done, _ = await asyncio.wait(
                (accept_task, receive_task), return_when=asyncio.FIRST_COMPLETED
            )
            if receive_task in done:
                message = receive_task.result()
                if _client_has_closed(message, session):
                    return
                frame = message.get("bytes")
                if frame is None:
                    raise ValueError("expected a binary ADB packet or close message")
                packet = _adb_packet(frame)
                if packet is None:
                    receive_task = asyncio.create_task(websocket.receive())
                    continue
                pending_bytes += len(packet)
                if pending_bytes > _MAX_PENDING_BYTES:
                    raise ValueError("too much ADB data before the loopback connection")
                pending_packets.append(packet)
                receive_task = asyncio.create_task(websocket.receive())
            if accept_task in done:
                break

        reader = session.reader
        writer = session.writer
        if reader is None or writer is None:
            raise ConnectionError("ADB server did not establish the loopback connection")

        for packet in pending_packets:
            writer.write(packet)
        if pending_packets:
            await writer.drain()
            for _packet in pending_packets:
                if not await _renew_lease(session, websocket, send_lock):
                    return

        tcp_to_websocket_task = asyncio.create_task(
            _forward_tcp_packets(reader, websocket, send_lock, session)
        )
        websocket_to_tcp_task = asyncio.create_task(
            _forward_websocket_packets(websocket, writer, receive_task, session, send_lock)
        )
        done, _ = await asyncio.wait(
            (tcp_to_websocket_task, websocket_to_tcp_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            task.result()
    except WebSocketDisconnect as error:
        _client_has_closed(
            {"type": "websocket.disconnect", "code": error.code, "reason": error.reason}, session
        )
        return
    finally:
        for task in (
            accept_task,
            receive_task,
            tcp_to_websocket_task,
            websocket_to_tcp_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (
                    accept_task,
                    receive_task,
                    tcp_to_websocket_task,
                    websocket_to_tcp_task,
                )
                if task is not None
            ),
            return_exceptions=True,
        )


async def _expire_session(
    websocket: WebSocket,
    session: BridgeSession,
    send_lock: asyncio.Lock,
) -> None:
    while True:
        remaining_seconds = session.remaining_seconds()
        if remaining_seconds <= 0:
            session.close_reason = session.expiration_reason()
            await _close_websocket(websocket, _CLOSE_SESSION_EXPIRED, send_lock)
            return
        await _sleep(remaining_seconds)


async def _renew_lease(
    session: BridgeSession,
    websocket: WebSocket,
    send_lock: asyncio.Lock,
) -> bool:
    if session.renew():
        return True
    session.close_reason = session.expiration_reason()
    await _close_websocket(websocket, _CLOSE_SESSION_EXPIRED, send_lock)
    return False


async def _close_websocket(
    websocket: WebSocket,
    code: int,
    send_lock: asyncio.Lock,
) -> None:
    async def close_locked() -> None:
        async with send_lock:
            await websocket.close(code=code)

    try:
        await asyncio.wait_for(close_locked(), timeout=_WEBSOCKET_SEND_TIMEOUT_SECONDS)
    except (RuntimeError, TimeoutError, WebSocketDisconnect):
        pass


@router.websocket("/session", dependencies=[Depends(require_bridge_access)])
async def open_bridge_session(websocket: WebSocket) -> None:
    if not _client_is_loopback(websocket):
        await websocket.close(code=_CLOSE_FORBIDDEN_REMOTE)
        return

    await websocket.accept()
    session: BridgeSession | None = None
    tasks: list[asyncio.Task[object]] = []
    send_lock = asyncio.Lock()
    try:
        identity = getattr(getattr(websocket, "state", None), "identity", None)
        session = await bridge_session_service.create_session(
            owner=identity.email if identity else None
        )
        await _send_json(
            websocket,
            {
                "type": "session_leased",
                "session_id": session.session_id,
                "listener": {"host": "127.0.0.1", "port": session.port},
                "expires_in_seconds": session.remaining_seconds(),
            },
            send_lock,
        )

        connect_task = asyncio.create_task(bridge_session_service.connect(session))
        relay_task = asyncio.create_task(_relay_packets(websocket, session, send_lock))
        expiry_task = asyncio.create_task(_expire_session(websocket, session, send_lock))
        tasks.extend((connect_task, relay_task, expiry_task))

        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if expiry_task in done:
            expiry_task.result()
            return
        if relay_task in done:
            relay_task.result()
            return
        if connect_task in done:
            serial = connect_task.result()
            await _send_json(
                websocket,
                {"type": "device_attached", "serial": serial},
                send_lock,
            )
            done, _ = await asyncio.wait(
                (relay_task, expiry_task), return_when=asyncio.FIRST_COMPLETED
            )
            if expiry_task in done:
                expiry_task.result()
                return
            relay_task.result()
    except WebSocketDisconnect as error:
        if session is not None:
            _client_has_closed(
                {"type": "websocket.disconnect", "code": error.code, "reason": error.reason},
                session,
            )
        return
    except Exception:
        if session is not None:
            session.close_reason = "error"
        logger.exception("Device bridge session failed")
        await _close_websocket(websocket, _CLOSE_BRIDGE_ERROR, send_lock)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        async def finish_session() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
            if session is not None:
                await bridge_session_service.revoke(session.session_id)

        cleanup_task = asyncio.create_task(finish_session())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise
