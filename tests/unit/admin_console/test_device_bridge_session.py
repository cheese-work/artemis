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

"""Tests for the loopback WebSocket device-bridge ADB relay.

Covers: relay forwarding, same-origin boundaries, and session cleanup.
"""

import asyncio
import socket
import struct
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from apps.admin_console.routers import device_bridge
import apps.admin_console.services.bridge_session_service as bridge_session_service_module
from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services.bridge_session_service import (
    BridgeSession,
    BridgeSessionService,
    bridge_session_service,
)

PATH = "/api/device-bridge/session"

# starlette's TestClient.websocket_connect always dials "ws://testserver"
# regardless of base_url, so the Host header must be overridden explicitly on
# every call or SameOriginBoundaryMiddleware rejects it as an unrecognized
# (DNS-rebinding-shaped) hostname before the request reaches this router.
_HOST_HEADER = {"Host": "127.0.0.1"}


def _adb_packet(command: bytes, payload: bytes = b"", checksum: int = 0) -> bytes:
    command_word = int.from_bytes(command, "little")
    header = struct.pack(
        "<6I", command_word, 0, 0, len(payload), checksum, command_word ^ 0xFFFFFFFF
    )
    return header + payload


def _assert_listener_closed(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", port))


@pytest.fixture
def _mock_adb(monkeypatch):
    calls = []

    async def fake_adb_command(*arguments):
        calls.append(arguments)
        return f"connected to {arguments[1]}" if arguments[0] == "connect" else "disconnected"

    monkeypatch.setattr(bridge_session_service_module, "_run_adb_command", fake_adb_command)
    return calls


@pytest.fixture
def loopback_client():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000))


@pytest.fixture
def remote_client():
    return TestClient(proxy_aware_app, client=("203.0.113.5", 50000))


def test_loopback_session_does_not_require_lifecycle_bearer(loopback_client, _mock_adb):
    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        assert ws.receive_json()["type"] == "session_leased"
        assert ws.receive_json()["type"] == "device_attached"
        ws.send_text("close")


def test_verified_nonadmin_can_open_local_bridge(loopback_client, monkeypatch, _mock_adb):
    from apps.admin_console.services.bridge_session_service import bridge_session_service
    from apps.admin_console.core.access_control import AccessConfig
    from apps.admin_console.server import app

    revoked = threading.Event()
    revoke = bridge_session_service.revoke

    async def observe_revoke(session_id):
        await revoke(session_id)
        revoked.set()

    monkeypatch.setattr(bridge_session_service, "revoke", observe_revoke)

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(return_value={"email": "qa@example.com"})
    monkeypatch.setattr(app.state, "access_verifier", verifier)

    with loopback_client.websocket_connect(
        PATH,
        headers={**_HOST_HEADER, "Cf-Access-Jwt-Assertion": "synthetic-fixture-token"},
    ) as ws:
        assert ws.receive_json()["type"] == "session_leased"
        assert ws.receive_json()["type"] == "device_attached"
        ws.send_text("close")

    assert revoked.wait(1.0)


def test_non_loopback_client_is_rejected(remote_client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with remote_client.websocket_connect(PATH, headers=_HOST_HEADER):
            pass
    assert exc_info.value.code == 4003


def test_forwarded_client_without_cloudflare_jwt_is_read_only(loopback_client):
    headers = {
        **_HOST_HEADER,
        "Origin": "https://127.0.0.1",
        "X-Forwarded-For": "203.0.113.7",
        "X-Forwarded-Proto": "https",
    }
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with loopback_client.websocket_connect(PATH, headers=headers):
            pass
    assert exc_info.value.code == 1008


def test_bad_host_and_origin_are_rejected(loopback_client):
    with pytest.raises(WebSocketDisconnect) as host_error:
        with loopback_client.websocket_connect(PATH, headers={"Host": "attacker.invalid"}):
            pass
    assert host_error.value.code == 1008

    with pytest.raises(WebSocketDisconnect) as origin_error:
        with loopback_client.websocket_connect(
            PATH, headers={**_HOST_HEADER, "Origin": "https://attacker.invalid"}
        ):
            pass
    assert origin_error.value.code == 1008


def test_lease_connect_packet_relay_and_close(loopback_client, monkeypatch, _mock_adb):
    peer = {}
    server_packet = _adb_packet(b"CNXN", b"server-banner", checksum=0)
    client_packet = _adb_packet(b"CNXN", b"client-banner", checksum=0)

    async def fake_adb_command(*arguments):
        _mock_adb.append(arguments)
        if arguments[0] == "connect":
            host, port = arguments[1].split(":")
            reader, writer = await asyncio.open_connection(host, int(port))
            peer["reader"] = reader
            peer["writer"] = writer
            writer.write(server_packet[:7])
            await writer.drain()
            writer.write(server_packet[7:])
            await writer.drain()
            received = await asyncio.wait_for(reader.readexactly(len(client_packet)), 1)
            assert received == client_packet
            return f"connected to {arguments[1]}"
        writer = peer.get("writer")
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        return "disconnected"

    monkeypatch.setattr(bridge_session_service_module, "_run_adb_command", fake_adb_command)

    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        leased = ws.receive_json()
        assert leased["type"] == "session_leased"
        session_id = leased["session_id"]
        assert leased["listener"]["host"] == "127.0.0.1"
        port = leased["listener"]["port"]
        assert isinstance(port, int) and port > 0

        ws.send_bytes(b"")
        assert ws.receive_bytes() == server_packet
        ws.send_bytes(client_packet)
        attached = ws.receive_json()
        assert attached == {"type": "device_attached", "serial": f"127.0.0.1:{port}"}
        ws.send_text("close")

    assert _mock_adb == [("connect", f"127.0.0.1:{port}"), ("disconnect", f"127.0.0.1:{port}")]
    assert asyncio.run(bridge_session_service.get(session_id)) is None
    _assert_listener_closed(port)


def test_loopback_session_binds_listener_and_closes(loopback_client, _mock_adb):
    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        payload = ws.receive_json()
        assert payload["type"] == "session_leased"
        session_id = payload["session_id"]
        assert payload["listener"]["host"] == "127.0.0.1"
        port = payload["listener"]["port"]
        assert isinstance(port, int) and port > 0

        ws.receive_json()

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        probe.connect(("127.0.0.1", port))
        ws.send_text("close")
        probe.close()

    assert asyncio.run(bridge_session_service.get(session_id)) is None
    _assert_listener_closed(port)


def test_disconnect_without_close_message_still_revokes_session(
    loopback_client, monkeypatch, _mock_adb
):
    revoked = threading.Event()
    revoke = bridge_session_service.revoke

    async def observe_revoke(session_id):
        await revoke(session_id)
        revoked.set()

    monkeypatch.setattr(bridge_session_service, "revoke", observe_revoke)
    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        payload = ws.receive_json()
        session_id = payload["session_id"]
        port = payload["listener"]["port"]
        ws.receive_json()
        ws.close()
        assert revoked.wait(1.0)

    assert asyncio.run(bridge_session_service.get(session_id)) is None
    assert [call[0] for call in _mock_adb] == ["connect", "disconnect"]
    _assert_listener_closed(port)


def test_no_listener_survives_after_multiple_sessions_open_and_close(loopback_client, _mock_adb):
    ports = []
    for _ in range(3):
        with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
            ports.append(ws.receive_json()["listener"]["port"])
            ws.receive_json()
            ws.send_text("close")

    active = asyncio.run(bridge_session_service.active_session_ids())
    assert active == set()
    assert [call[0] for call in _mock_adb] == ["connect", "disconnect"] * 3
    for port in ports:
        _assert_listener_closed(port)


def test_expired_session_times_out_and_disconnects_adb(loopback_client, monkeypatch, _mock_adb):
    monkeypatch.setenv("ARTEMIS_BRIDGE_SESSION_TTL_SECONDS", "0.05")

    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        payload = ws.receive_json()
        session_id = payload["session_id"]
        port = payload["listener"]["port"]
        ws.receive_json()

        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 4008

    assert _mock_adb[0][0] == "connect"
    assert _mock_adb[1][0] == "disconnect"
    assert asyncio.run(bridge_session_service.get(session_id)) is None
    _assert_listener_closed(port)


def test_expired_session_revokes_when_tcp_drain_is_backpressured(monkeypatch):
    class BackpressuredWriter:
        def __init__(self):
            self.draining = asyncio.Event()

        def write(self, _packet):
            pass

        async def drain(self):
            self.draining.set()
            await asyncio.Event().wait()

    class FakeWebSocket:
        def __init__(self):
            self.scope = {"client": ("127.0.0.1", 50000)}
            self.client = ("127.0.0.1", 50000)
            self.messages = asyncio.Queue()
            self.closed = asyncio.Event()

        async def accept(self):
            pass

        async def send_json(self, _message):
            pass

        async def send_bytes(self, _message):
            pass

        async def receive(self):
            return await self.messages.get()

        async def close(self, code):
            self.messages.put_nowait({"type": "websocket.disconnect", "code": code})
            self.closed.set()

    class FakeSessionService:
        def __init__(self, session):
            self.session = session
            self.revoked = []

        async def create_session(self):
            return self.session

        async def connect(self, session):
            return session.serial

        async def revoke(self, session_id):
            self.revoked.append(session_id)

    async def scenario():
        websocket = FakeWebSocket()
        writer = BackpressuredWriter()
        session = BridgeSession(
            session_id="backpressured-relay",
            port=43210,
            reader=asyncio.StreamReader(),
            writer=writer,
            expires_at=time.monotonic() + 0.05,
        )
        session.connected.set()
        websocket.messages.put_nowait({"type": "websocket.receive", "bytes": _adb_packet(b"CNXN")})
        service = FakeSessionService(session)
        monkeypatch.setattr(device_bridge, "bridge_session_service", service)

        route_task = asyncio.create_task(device_bridge.open_bridge_session(websocket))
        try:
            await asyncio.wait_for(writer.draining.wait(), 1)
            await asyncio.wait_for(websocket.closed.wait(), 1)
            done, _ = await asyncio.wait((route_task,), timeout=0.1)
            assert route_task in done
            assert service.revoked == [session.session_id]
        finally:
            if not route_task.done():
                route_task.cancel()
            await asyncio.gather(route_task, return_exceptions=True)

    asyncio.run(scenario())


def test_adb_commands_are_pinned_to_the_local_server(monkeypatch):
    invocations = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"connected to 127.0.0.1:43210", b""

    async def fake_subprocess(*arguments, **keywords):
        invocations.append((arguments, keywords["env"]))
        return FakeProcess()

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:203.0.113.9:5037")
    monkeypatch.setattr(
        bridge_session_service_module,
        "find_adb",
        lambda: "offline-adb-sentinel",
    )
    monkeypatch.setattr(
        bridge_session_service_module.asyncio,
        "create_subprocess_exec",
        fake_subprocess,
    )

    async def scenario():
        await bridge_session_service_module._run_adb_command("connect", "127.0.0.1:43210")
        await bridge_session_service_module._run_adb_command("disconnect", "127.0.0.1:43210")

    asyncio.run(scenario())

    assert len(invocations) == 2
    for arguments, environment in invocations:
        assert arguments[:5] == (
            "offline-adb-sentinel",
            "-H",
            "127.0.0.1",
            "-P",
            "5037",
        )
        assert arguments[5] in {"connect", "disconnect"}
        assert arguments[6] == "127.0.0.1:43210"
        assert environment["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"


def test_failed_disconnect_closes_accepted_writer_before_waiting_for_listener(monkeypatch):
    async def failed_disconnect(*_arguments):
        raise RuntimeError("offline injected disconnect failure")

    async def scenario():
        service = BridgeSessionService()
        session = await service.create_session()
        _peer_reader, peer_writer = await asyncio.open_connection("127.0.0.1", session.port)
        await asyncio.wait_for(session.connected.wait(), 1)
        session.adb_connect_attempted = True
        monkeypatch.setattr(
            bridge_session_service_module,
            "_run_adb_command",
            failed_disconnect,
        )

        try:
            await asyncio.wait_for(service.revoke(session.session_id), 1)
            assert session.writer.is_closing()
            assert not session.listener.is_serving()
            assert await service.get(session.session_id) is None
        finally:
            session.listener.close()
            if session.writer is not None and not session.writer.is_closing():
                session.writer.close()
            if not peer_writer.is_closing():
                peer_writer.close()
            await asyncio.wait_for(
                asyncio.gather(
                    session.listener.wait_closed(),
                    session.writer.wait_closed(),
                    peer_writer.wait_closed(),
                ),
                1,
            )

    asyncio.run(scenario())


def test_failed_disconnect_force_closes_buffered_accepted_socket(monkeypatch):
    disconnect_calls = []

    async def failed_disconnect(*arguments):
        disconnect_calls.append(arguments)
        raise RuntimeError("offline injected disconnect failure")

    async def scenario():
        service = BridgeSessionService()
        session = await service.create_session()
        _peer_reader, peer_writer = await asyncio.open_connection("127.0.0.1", session.port)
        await asyncio.wait_for(session.connected.wait(), 1)
        session.adb_connect_attempted = True
        accepted_socket = session.writer.get_extra_info("socket")
        peer_socket = peer_writer.get_extra_info("socket")
        accepted_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        peer_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        session.writer.write(_adb_packet(b"CNXN", b"x" * (1024 * 1024)))
        await asyncio.sleep(0.05)
        assert session.writer.transport.get_write_buffer_size() > 0
        monkeypatch.setattr(
            bridge_session_service_module,
            "_run_adb_command",
            failed_disconnect,
        )

        try:
            await asyncio.wait_for(
                service.revoke(session.session_id),
                bridge_session_service_module.STREAM_CLOSE_TIMEOUT_SECONDS * 2 + 1,
            )
            assert disconnect_calls == [("disconnect", session.serial)]
            assert accepted_socket.fileno() == -1
            assert await service.get(session.session_id) is None
        finally:
            session.listener.close()
            session.writer.transport.abort()
            peer_writer.close()
            await asyncio.wait_for(
                asyncio.gather(session.listener.wait_closed(), peer_writer.wait_closed()),
                1,
            )

    asyncio.run(scenario())


def test_failed_adb_connect_disconnects_and_releases_listener(
    loopback_client, monkeypatch, _mock_adb
):
    async def failed_adb_command(*arguments):
        _mock_adb.append(arguments)
        return "failed to connect" if arguments[0] == "connect" else "disconnected"

    monkeypatch.setattr(bridge_session_service_module, "_run_adb_command", failed_adb_command)

    with loopback_client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        payload = ws.receive_json()
        session_id = payload["session_id"]
        port = payload["listener"]["port"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()

    assert [call[0] for call in _mock_adb] == ["connect", "disconnect"]
    assert asyncio.run(bridge_session_service.get(session_id)) is None
    _assert_listener_closed(port)
