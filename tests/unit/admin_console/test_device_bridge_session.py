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

"""Tests for the authenticated WSS device-bridge session-lease stub (CHE-786).

Covers: unauthorized rejection, loopback-only binding, expiration/disconnect
cleanup, and no leaked listener after the connection ends.
"""

import asyncio
import socket

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.admin_console.server import app
from apps.admin_console.services.bridge_session_service import bridge_session_service

TOKEN = "test-lifecycle-token"
PATH = "/api/device-bridge/session"

# starlette's TestClient.websocket_connect always dials "ws://testserver"
# regardless of base_url, so the Host header must be overridden explicitly on
# every call or SameOriginBoundaryMiddleware rejects it as an unrecognized
# (DNS-rebinding-shaped) hostname before the request reaches this router.
_HOST_HEADER = {"Host": "127.0.0.1"}


def _auth_headers(token: str = TOKEN) -> dict[str, str]:
    return {**_HOST_HEADER, "Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _lifecycle_token():
    previous = getattr(app.state, "lifecycle_token", None)
    app.state.lifecycle_token = TOKEN
    yield
    app.state.lifecycle_token = previous


@pytest.fixture
def loopback_client():
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def remote_client():
    return TestClient(app, client=("203.0.113.5", 50000))


def test_missing_bearer_token_is_rejected(loopback_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER):
            pass
    assert exc_info.value.code == 4001


def test_wrong_bearer_token_is_rejected(loopback_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with loopback_client.websocket_connect(PATH, headers=_auth_headers("not-the-token")):
            pass
    assert exc_info.value.code == 4001


def test_non_loopback_client_is_rejected_even_with_valid_token(remote_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with remote_client.websocket_connect(PATH, headers=_auth_headers()):
            pass
    assert exc_info.value.code == 4003


def test_authorized_loopback_session_binds_loopback_only_listener(loopback_client):
    with loopback_client.websocket_connect(PATH, headers=_auth_headers()) as ws:
        payload = ws.receive_json()
        assert payload["type"] == "session_leased"
        session_id = payload["session_id"]
        assert payload["listener"]["host"] == "127.0.0.1"
        port = payload["listener"]["port"]
        assert isinstance(port, int) and port > 0

        # Binding on 127.0.0.1 (not 0.0.0.0) leaves no wildcard/external
        # interface to assert against, so confirm the listener is reachable
        # on loopback rather than trying to prove a negative over the network.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(("127.0.0.1", port))
            connected = True
        except OSError:
            connected = False
        finally:
            probe.close()
        assert connected, "expected the leased loopback listener to accept a local connection"

        ws.send_text("close")

    # Session must be revoked (and its listener closed) once the socket closes.
    assert asyncio.run(bridge_session_service.get(session_id)) is None


def test_disconnect_without_close_message_still_revokes_session(loopback_client):
    with loopback_client.websocket_connect(PATH, headers=_auth_headers()) as ws:
        payload = ws.receive_json()
        session_id = payload["session_id"]
        # Exit the context without sending "close" — simulates an abrupt
        # client-side socket drop rather than a graceful close handshake.

    assert asyncio.run(bridge_session_service.get(session_id)) is None


def test_no_listener_survives_after_multiple_sessions_open_and_close(loopback_client):
    for _ in range(3):
        with loopback_client.websocket_connect(PATH, headers=_auth_headers()) as ws:
            ws.receive_json()
            ws.send_text("close")

    active = asyncio.run(bridge_session_service.active_session_ids())
    assert active == set()


def test_expired_session_times_out_the_websocket_handler(loopback_client, monkeypatch):
    """The handler closes with the expiry code once the lease's TTL elapses,
    without needing the client to send anything."""
    monkeypatch.setenv("ARTEMIS_BRIDGE_SESSION_TTL_SECONDS", "0.05")

    with loopback_client.websocket_connect(PATH, headers=_auth_headers()) as ws:
        payload = ws.receive_json()
        session_id = payload["session_id"]

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 4008

    # Revocation still runs on the timeout exit path: no leftover lease.
    assert asyncio.run(bridge_session_service.get(session_id)) is None
