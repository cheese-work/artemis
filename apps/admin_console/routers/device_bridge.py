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

"""Authenticated WSS session-lease stub for the browser device-bridge.

Scope (CHE-786): authorize one bearer-authenticated session, bind an
ephemeral loopback-only TCP listener, and reliably tear it down on explicit
close, socket disconnect, or timeout. This route does not parse or emulate
ADB, does not enumerate a device, and does not invoke ``adb``. A browser-side
WebUSB/ADB protocol client is a separate, not-yet-vetted follow-up (CHE-785).
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

try:
    from admin_console.services.bridge_session_service import bridge_session_service
except ImportError:
    from apps.admin_console.services.bridge_session_service import bridge_session_service

router = APIRouter(prefix="/api/device-bridge", tags=["device-bridge"])

# WebSocket close codes (RFC 6455 private-use range, 4000-4999).
_CLOSE_UNAUTHORIZED = 4001
_CLOSE_FORBIDDEN_REMOTE = 4003
_CLOSE_SESSION_EXPIRED = 4008


def _client_is_loopback(websocket: WebSocket) -> bool:
    client = websocket.client
    host = client.host if client else None
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _bearer_token_is_valid(websocket: WebSocket) -> bool:
    expected = getattr(websocket.app.state, "lifecycle_token", None)
    if not isinstance(expected, str) or not expected:
        return False

    header = websocket.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False

    return secrets.compare_digest(expected, supplied)


@router.websocket("/session")
async def open_bridge_session(websocket: WebSocket) -> None:
    """Lease one authenticated, loopback-only session for its WSS lifetime.

    The lease is a listener-and-identity pair: it exists exactly as long as
    this WebSocket connection does, and is revoked on every exit path
    (explicit close message, client disconnect, or TTL timeout) so no
    listener can outlive its authorization.
    """
    if not _client_is_loopback(websocket):
        await websocket.close(code=_CLOSE_FORBIDDEN_REMOTE)
        return

    if not _bearer_token_is_valid(websocket):
        await websocket.close(code=_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept()

    session = await bridge_session_service.create_session()
    try:
        await websocket.send_json(
            {
                "type": "session_leased",
                "session_id": session.session_id,
                "listener": {"host": "127.0.0.1", "port": session.port},
                "expires_in_seconds": session.remaining_seconds(),
            }
        )

        while True:
            if session.is_expired:
                await websocket.close(code=_CLOSE_SESSION_EXPIRED)
                break
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=session.remaining_seconds()
                )
            except TimeoutError:
                await websocket.close(code=_CLOSE_SESSION_EXPIRED)
                break
            except WebSocketDisconnect:
                break

            if message == "close":
                await websocket.close(code=1000)
                break
    finally:
        await bridge_session_service.revoke(session.session_id)
