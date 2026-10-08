"""Accessibility helper over an adb stream, end to end against fake adb servers (CHE-1094).

Replaces ``adb forward`` + loopback HTTP: the helper must be reachable through
whichever adb server the endpoint names, the same serial behind two servers must
reach two different phones, and no stream may outlive its request.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import urllib.error

import pytest

from artemis.clients.accessibility_client import (
    TOKEN_HEADER,
    AccessibilityClient,
    HelperRequestError,
)
from artemis.runtime import helper_stream
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.runtime.helper_manager import (
    DEVICE_PORT,
    SERVICE_NAME,
    AccessibilityHelperManager,
    HelperUnavailable,
    helper_manager_for,
)
from tests.support.fake_adb import FakeAdbServer, http_service

SERIAL = "emulator-5554"
REAL_ADB = shutil.which("adb")
needs_adb = pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")


def _xml(name: str) -> str:
    return f'<hierarchy rotation="0"><node text="{name}" bounds="[0,0][10,10]"/></hierarchy>'


def install_helper(server: FakeAdbServer, name: str, *, token_required: bool = True) -> dict:
    """Give ``SERIAL`` on ``server`` a running helper that answers as ``name``."""
    state = {"tokens_seen": [], "token": None, "requests": []}
    device = server.devices.get(SERIAL) or server.add_device(SERIAL)

    def authorized(headers_token: str | None) -> bool:
        return not token_required or headers_token == state["token"]

    def ping(_method, _body):
        return 200, json.dumps(
            {
                "success": True,
                "name": name,
                "version_code": 2,
                "version_name": "1.1.0",
                "protocol_version": 2,
                "auth_required": token_required,
                "token_set": state["token"] is not None,
            }
        ).encode()

    def dump_xml(_method, _body):
        return 200, _xml(name).encode()

    device.services[DEVICE_PORT] = http_service({"/ping": ping, "/dump_xml": dump_xml})

    def shell(dev, command: str) -> bytes:
        if "dumpsys package" in command:
            return b"Package [com.artemis.helper]\n  versionCode=2 minSdk=24\n"
        if "settings get secure enabled_accessibility_services" in command:
            return f"{SERVICE_NAME}\n".encode()
        if "am broadcast" in command:
            state["token"] = command.split("--es token ")[1].split()[0]
            state["tokens_seen"].append(state["token"])
            return b"Broadcast completed: result=0\n"
        if command.startswith("ps "):
            return b"PID ARGS\n"
        return b""

    device.shell_handler = shell
    return state


@pytest.fixture
def servers(fake_adb_server_factory):
    alpha = fake_adb_server_factory("alpha")
    beta = fake_adb_server_factory("beta")
    return alpha, beta


def test_request_returns_the_body_and_releases_its_stream(servers):
    alpha, _ = servers
    install_helper(alpha, "alpha")
    transport = EndpointTransport(alpha.endpoint)

    body = helper_stream.request(transport, SERIAL, DEVICE_PORT, "GET", "/dump_xml")

    assert body.decode() == _xml("alpha")
    assert transport.open_stream_count == 0
    assert "tcp:18888" in alpha.request_strings(serial=SERIAL)


def test_http_error_status_surfaces_as_httperror_with_the_body(servers):
    alpha, _ = servers
    install_helper(alpha, "alpha")
    transport = EndpointTransport(alpha.endpoint)

    with pytest.raises(urllib.error.HTTPError) as caught:
        helper_stream.request(transport, SERIAL, DEVICE_PORT, "GET", "/missing")

    assert caught.value.code == 404 and caught.value.read() == b"not found"
    assert transport.open_stream_count == 0


def test_a_device_without_the_service_is_a_url_error_not_a_leak(servers):
    alpha, _ = servers
    alpha.add_device(SERIAL)  # no helper service listening
    transport = EndpointTransport(alpha.endpoint)

    with pytest.raises(urllib.error.URLError):
        helper_stream.request(transport, SERIAL, DEVICE_PORT, "GET", "/ping")

    assert transport.open_stream_count == 0


def test_an_unknown_device_is_a_url_error(servers):
    alpha, _ = servers
    transport = EndpointTransport(alpha.endpoint)

    with pytest.raises(urllib.error.URLError):
        helper_stream.request(transport, "nope", DEVICE_PORT, "GET", "/ping")

    assert transport.open_stream_count == 0


@needs_adb
def test_same_serial_on_two_servers_reaches_two_phones(servers, tmp_path, monkeypatch):
    alpha, beta = servers
    install_helper(alpha, "alpha")
    install_helper(beta, "beta")
    monkeypatch.setattr(
        "artemis.runtime.helper_manager.get_temp_dir", lambda _name: tmp_path / "mutex"
    )
    clients = {}
    for name, server in (("alpha", alpha), ("beta", beta)):
        transport = EndpointTransport(server.endpoint, adb_path=REAL_ADB)
        manager = AccessibilityHelperManager(
            transport=transport,
            token_path=tmp_path / name / "token",
            sleep=lambda _s: None,
        )
        clients[name] = AccessibilityClient(
            SERIAL,
            manager=manager,
            provision_on_connect=False,
            transport=transport,
        )

    def read(name: str) -> str:
        client = clients[name]
        client._ensure_session()
        return client.get_hierarchy()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = dict(zip(("alpha", "beta"), pool.map(read, ("alpha", "beta"))))

    assert results == {"alpha": _xml("alpha"), "beta": _xml("beta")}
    for name, server in (("alpha", alpha), ("beta", beta)):
        requests = server.request_strings(serial=SERIAL)
        assert "tcp:18888" in requests
        assert not any(r.startswith("host:forward") for r in server.request_strings())
        assert clients[name]._manager.session(SERIAL).endpoint == server.endpoint.identity
    assert clients["alpha"]._manager.session(SERIAL) is not clients["beta"]._manager.session(SERIAL)


@needs_adb
def test_session_token_is_pushed_and_sent_with_requests(servers, tmp_path, monkeypatch):
    alpha, _ = servers
    state = install_helper(alpha, "alpha")
    monkeypatch.setattr(
        "artemis.runtime.helper_manager.get_temp_dir", lambda _name: tmp_path / "mutex"
    )
    transport = EndpointTransport(alpha.endpoint, adb_path=REAL_ADB)
    manager = AccessibilityHelperManager(
        transport=transport, token_path=tmp_path / "token", sleep=lambda _s: None
    )

    session = manager.attach(SERIAL, provision=False)
    raw = manager.http(SERIAL, "/dump_xml", None, {TOKEN_HEADER: session.token})

    assert raw.decode() == _xml("alpha")
    assert state["tokens_seen"] == [session.token]


@needs_adb
def test_attach_to_an_offline_host_is_helper_unavailable_and_spawns_nothing(monkeypatch, tmp_path):
    import socket

    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    from artemis.runtime.adb_endpoint import AdbEndpoint

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    endpoint = AdbEndpoint.create("127.0.0.1", dead_port, host_id="lab-1")
    manager = AccessibilityHelperManager(
        transport=EndpointTransport(endpoint, adb_path=REAL_ADB),
        token_path=tmp_path / "token",
        sleep=lambda _s: None,
    )
    monkeypatch.setattr(
        "artemis.runtime.helper_manager.get_temp_dir", lambda _name: tmp_path / "mutex"
    )

    with pytest.raises(HelperUnavailable):
        manager.attach(SERIAL, provision=False)


def test_helper_manager_for_returns_one_manager_per_endpoint(servers):
    alpha, beta = servers

    assert helper_manager_for(alpha.endpoint) is helper_manager_for(alpha.endpoint)
    assert helper_manager_for(alpha.endpoint) is not helper_manager_for(beta.endpoint)


def test_client_surfaces_http_errors_without_repairs(servers, tmp_path):
    alpha, _ = servers
    install_helper(alpha, "alpha")
    transport = EndpointTransport(alpha.endpoint)
    manager = AccessibilityHelperManager(transport=transport, token_path=tmp_path / "token")
    client = AccessibilityClient(SERIAL, manager=manager, transport=transport)

    class Session:
        token = "t"

    client._session = Session()  # skip attach: this test is about the request path
    with pytest.raises(HelperRequestError) as caught:
        client._http("/nope")

    assert caught.value.status == 404
    assert transport.open_stream_count == 0
