"""The fake adb server is faithful to the pinned clients (CHE-1094).

Pins what the pinned ``adb`` binary and ``adbutils`` send and accept against
:mod:`tests.support.fake_adb`, and that today's explicit-endpoint adb path
(``AdbSession``) reaches the server it names. Everything here points clients at
the fake explicitly; nothing touches a real adb server or device.
"""

from __future__ import annotations

import shutil

import adbutils
from adbutils import AdbClient, Network
import pytest

from artemis.runtime.adb_endpoint import AdbSession
from artemis.runtime.device_pool import DevicePool
from tests.support.fake_adb import http_service

REAL_ADB = shutil.which("adb")
needs_adb = pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")


@pytest.fixture
def server(fake_adb_server_factory):
    server = fake_adb_server_factory("alpha")
    device = server.add_device("emulator-5554")
    device.screencap = b"\x89PNG-alpha"
    device.services[7912] = http_service(
        {"/ping": lambda _m, _b: (200, b'{"success": true, "name": "alpha"}')}
    )
    return server


def test_adbutils_lists_devices_and_runs_shell_through_the_fake(server):
    client = AdbClient(server.host, server.port)

    assert client.server_version() == 41
    assert [d.serial for d in client.device_list()] == ["emulator-5554"]
    assert client.device("emulator-5554").shell("echo hi").strip() == "hi"


def test_adbutils_tcp_stream_reaches_the_device_service(server):
    device = AdbClient(server.host, server.port).device("emulator-5554")

    stream = device.create_connection(Network.TCP, 7912)
    try:
        stream.sendall(b"GET /ping HTTP/1.0\r\n\r\n")
        answer = b""
        while chunk := stream.recv(4096):
            answer += chunk
    finally:
        stream.close()

    assert b'"name": "alpha"' in answer
    assert "tcp:7912" in server.request_strings(serial="emulator-5554")


@needs_adb
def test_adb_session_reaches_the_endpoint_it_names(server):
    session = AdbSession(server.endpoint, adb_path=REAL_ADB)

    listing = session.run(["devices", "-l"], capture_output=True, text=True, timeout=15)
    shell = session.run(
        ["-s", "emulator-5554", "shell", "echo", "ok"], capture_output=True, text=True, timeout=15
    )
    screenshot = session.run(
        ["-s", "emulator-5554", "exec-out", "screencap", "-p"], capture_output=True, timeout=15
    )

    assert listing.returncode == 0
    assert [row[:2] for row in DevicePool._parse_device_lines(listing.stdout.splitlines())] == [
        ("emulator-5554", "device")
    ]
    assert shell.stdout.strip() == "ok"
    assert screenshot.stdout == b"\x89PNG-alpha"


@needs_adb
def test_the_pinned_adb_binary_matches_the_fake_protocol_version(server):
    """A client that disagrees on ``host:version`` would respawn a real adb server."""
    result = AdbSession(server.endpoint, adb_path=REAL_ADB).run(
        ["devices"], capture_output=True, text=True, timeout=15
    )

    assert result.returncode == 0
    assert "starting" not in (result.stdout + result.stderr).lower()
    assert not server.killed


def test_adbutils_global_client_reads_the_android_adb_server_environment(monkeypatch):
    """Why the process environment, not ``AdbEndpoint`` alone, has to name the server."""
    monkeypatch.setenv("ANDROID_ADB_SERVER_HOST", "192.0.2.9")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "6000")

    client = adbutils.AdbClient()

    assert (client.host, client.port) == ("192.0.2.9", 6000)
