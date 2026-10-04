"""Device-bridge relay through the real adb seam (CHE-1094, written before the refactor).

``test_device_bridge_session`` mocks ``_run_adb_command`` wholesale. This test
keeps the bridge's own adb calls: the pinned ``adb`` binary issues ``connect``
and ``disconnect`` against a fake adb server, which plays adb dialling out to the
bridge's loopback listener. It pins that the browser relay still carries the
adb handshake byte-for-byte after the adb call path moves into the endpoint
transport, and that ``connect``/``disconnect`` reach the *local* server.
"""

from __future__ import annotations

import shutil
import socket
import struct
import time

from fastapi.testclient import TestClient
import pytest

import apps.admin_console.services.bridge_session_service as bridge_session_service_module
from apps.admin_console.server import proxy_aware_app
from artemis.runtime.adb_endpoint import AdbEndpoint

REAL_ADB = shutil.which("adb")
PATH = "/api/device-bridge/session"
_HOST_HEADER = {"Host": "127.0.0.1"}

pytestmark = pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")


def _adb_packet(command: bytes, payload: bytes = b"") -> bytes:
    word = int.from_bytes(command, "little")
    return struct.pack("<6I", word, 0, 0, len(payload), 0, word ^ 0xFFFFFFFF) + payload


def test_relay_carries_the_adb_handshake_through_the_local_adb_server(
    monkeypatch, fake_adb_server_factory
):
    local = fake_adb_server_factory("local-adb")
    server_packet = _adb_packet(b"CNXN", b"adb-server-banner")
    client_packet = _adb_packet(b"CNXN", b"browser-banner")
    dialled: dict[str, socket.socket] = {}
    received: list[bytes] = []

    def dial_bridge(address: str) -> str:
        host, port = address.rsplit(":", 1)
        peer = socket.create_connection((host, int(port)), timeout=5)
        dialled["peer"] = peer
        peer.sendall(server_packet[:9])
        peer.sendall(server_packet[9:])
        reply = b""
        while len(reply) < len(client_packet):
            chunk = peer.recv(len(client_packet) - len(reply))
            if not chunk:
                break
            reply += chunk
        received.append(reply)
        return f"connected to {address}"

    def hang_up(address: str) -> str:
        peer = dialled.pop("peer", None)
        if peer is not None:
            peer.close()
        return f"disconnected {address}"

    local.on_connect = dial_bridge
    local.on_disconnect = hang_up
    monkeypatch.setattr(AdbEndpoint, "local", classmethod(lambda cls: local.endpoint))
    monkeypatch.setattr(bridge_session_service_module, "find_adb", lambda: REAL_ADB)

    client = TestClient(proxy_aware_app, client=("127.0.0.1", 50000))
    with client.websocket_connect(PATH, headers=_HOST_HEADER) as ws:
        leased = ws.receive_json()
        assert leased["type"] == "session_leased"
        port = leased["listener"]["port"]

        ws.send_bytes(b"")
        assert ws.receive_bytes() == server_packet
        ws.send_bytes(client_packet)
        attached = ws.receive_json()
        assert attached == {"type": "device_attached", "serial": f"127.0.0.1:{port}"}
        ws.send_text("close")
        # The bridge disconnects adb asynchronously; keep the app alive until it has.
        deadline = time.monotonic() + 5
        while f"host:disconnect:127.0.0.1:{port}" not in local.request_strings():
            assert time.monotonic() < deadline, local.request_strings()
            time.sleep(0.05)

    assert received == [client_packet]
    assert f"host:connect:127.0.0.1:{port}" in local.request_strings()
