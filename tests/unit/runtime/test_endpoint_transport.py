"""EndpointTransport: explicit-endpoint adb access (CHE-1094), against the fake adb server."""

from __future__ import annotations

import shutil
import subprocess

from adbutils import AdbError
import pytest

from artemis.runtime.adb_endpoint import (
    AdbEndpoint,
    AdbTarget,
    InvalidAdbEndpoint,
    current_adb_endpoint,
)
from artemis.runtime.endpoint_transport import (
    EndpointTransport,
    EndpointUnreachable,
    LocalOnlyOperation,
)
from tests.support.fake_adb import http_service

REAL_ADB = shutil.which("adb")
needs_adb = pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")


@pytest.fixture
def host_agent(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")


def _two_same_serial_servers(factory):
    servers = []
    for name in ("alpha", "beta"):
        server = factory(name)
        device = server.add_device("emulator-5554")
        device.screencap = f"png-{name}".encode()
        device.services[7912] = http_service(
            {
                "/ping": lambda _m, _b, name=name: (
                    200,
                    f'{{"success": true, "name": "{name}"}}'.encode(),
                )
            }
        )
        servers.append(server)
    return servers


# --------------------------------------------------------------------------- #
# Endpoint model
# --------------------------------------------------------------------------- #


def test_host_endpoints_are_refused_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("ARTEMIS_HOST_AGENT", raising=False)

    with pytest.raises(InvalidAdbEndpoint, match="disabled"):
        AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1")


def test_host_endpoint_identity_is_the_host_not_the_port(host_agent):
    first = AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1", generation=1)
    second = AdbEndpoint.create("127.0.0.1", 40001, host_id="lab-1", generation=2)

    assert first.identity == second.identity == "host:lab-1"
    assert first.mode == "host" and not first.persistable and not first.is_local_default
    assert AdbTarget(first, "emulator-5554").lock_key == "host:lab-1/emulator-5554"
    assert AdbEndpoint.from_mapping(first.to_dict()) == first


def test_host_endpoint_must_be_loopback_and_have_a_safe_id(host_agent):
    with pytest.raises(InvalidAdbEndpoint, match="loopback"):
        AdbEndpoint.create("192.0.2.7", 40000, host_id="lab-1")
    with pytest.raises(InvalidAdbEndpoint, match="Host id"):
        AdbEndpoint.create("127.0.0.1", 40000, host_id="../etc")


def test_apply_to_environment_sets_the_android_variables_for_adbutils(host_agent):
    environment: dict[str, str] = {"ARTEMIS_ADB_HOST_ID": "stale"}

    AdbEndpoint.create("192.0.2.7", 5555).apply_to_environment(environment)

    assert environment["ANDROID_ADB_SERVER_HOST"] == "192.0.2.7"
    assert environment["ANDROID_ADB_SERVER_PORT"] == "5555"
    assert "ARTEMIS_ADB_HOST_ID" not in environment

    AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1", generation=3).apply_to_environment(
        environment
    )
    assert environment["ARTEMIS_ADB_HOST_ID"] == "lab-1"
    assert environment["ARTEMIS_ADB_GENERATION"] == "3"


def test_current_endpoint_round_trips_a_host_endpoint_through_the_environment(
    host_agent, monkeypatch
):
    from artemis.config import settings

    endpoint = AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1", generation=2)
    environment: dict[str, str] = {}
    endpoint.apply_to_environment(environment)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 40000)

    assert current_adb_endpoint() == endpoint


# --------------------------------------------------------------------------- #
# Two endpoints, one serial
# --------------------------------------------------------------------------- #


def test_adbutils_calls_reach_only_their_own_endpoint(fake_adb_server_factory):
    alpha, beta = _two_same_serial_servers(fake_adb_server_factory)
    a, b = EndpointTransport(alpha.endpoint), EndpointTransport(beta.endpoint)

    assert a.device("emulator-5554").shell("echo hi").strip() == "hi"
    assert alpha.request_strings(serial="emulator-5554") != []
    assert beta.request_strings(serial="emulator-5554") == []

    out_b = b.device("emulator-5554").shell("uname")
    assert out_b.startswith("beta:emulator-5554")
    assert not any("uname" in r for r in alpha.request_strings())


def test_streams_reach_the_intended_device_service_and_are_released(fake_adb_server_factory):
    alpha, beta = _two_same_serial_servers(fake_adb_server_factory)
    answers = {}
    for name, server in (("alpha", alpha), ("beta", beta)):
        transport = EndpointTransport(server.endpoint)
        stream = transport.open_stream("emulator-5554", 7912)
        assert transport.open_stream_count == 1
        try:
            stream.sendall(b"GET /ping HTTP/1.0\r\n\r\n")
            data = b""
            while chunk := stream.recv(4096):
                data += chunk
        finally:
            transport.close_stream(stream)
        assert transport.open_stream_count == 0
        answers[name] = data

    assert b'"name": "alpha"' in answers["alpha"] and b'"name": "beta"' in answers["beta"]


@needs_adb
def test_subprocess_calls_reach_only_their_own_endpoint(fake_adb_server_factory):
    alpha, beta = _two_same_serial_servers(fake_adb_server_factory)
    a = EndpointTransport(alpha.endpoint, adb_path=REAL_ADB)
    b = EndpointTransport(beta.endpoint, adb_path=REAL_ADB)

    shot_a = a.run(["-s", "emulator-5554", "exec-out", "screencap", "-p"], capture_output=True)
    shot_b = b.run(["-s", "emulator-5554", "exec-out", "screencap", "-p"], capture_output=True)

    assert shot_a.stdout == b"png-alpha"
    assert shot_b.stdout == b"png-beta"
    assert sum("screencap" in r for r in alpha.request_strings()) == 1
    assert sum("screencap" in r for r in beta.request_strings()) == 1


def test_environment_points_children_at_the_endpoint(fake_adb_server_factory):
    alpha, _ = _two_same_serial_servers(fake_adb_server_factory)

    env = EndpointTransport(alpha.endpoint).environment({"PATH": "/bin"})

    assert env["ANDROID_ADB_SERVER_PORT"] == str(alpha.port)
    assert env["ADB_SERVER_SOCKET"] == f"tcp:127.0.0.1:{alpha.port}"
    assert env["PATH"] == "/bin"


def test_uiautomator2_connects_through_the_endpoints_device(fake_adb_server_factory):
    alpha, beta = _two_same_serial_servers(fake_adb_server_factory)

    with pytest.raises(AdbError):  # the fake has no sync service; u2 stops there
        EndpointTransport(beta.endpoint).u2_connect("emulator-5554")

    assert any("u2.jar" in r for r in beta.request_strings())
    assert alpha.request_strings(serial="emulator-5554") == []


# --------------------------------------------------------------------------- #
# Never spawn, never act locally for someone else's endpoint
# --------------------------------------------------------------------------- #


def _dead_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def spawn_trap(monkeypatch):
    """Fail the test if anything tries to start an adb server."""
    spawned: list[list[str]] = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def trap_run(argv, *args, **kwargs):
        spawned.append(list(argv))
        return real_run(["true"], *args, **kwargs)

    class TrapPopen(real_popen):
        def __init__(self, argv, *args, **kwargs):
            spawned.append(list(argv))
            super().__init__(["true"], *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", trap_run)
    monkeypatch.setattr(subprocess, "Popen", TrapPopen)
    return spawned


def test_unreachable_loopback_endpoint_never_spawns_a_local_server(host_agent, spawn_trap):
    endpoint = AdbEndpoint.create("127.0.0.1", _dead_port(), host_id="lab-1")
    transport = EndpointTransport(endpoint, adb_path="adb-sentinel")

    with pytest.raises(EndpointUnreachable, match="host offline"):
        transport.run(["devices"])
    with pytest.raises(EndpointUnreachable):
        transport.open_stream("emulator-5554", 7912)
    with pytest.raises(AdbError):  # adbutils' own spawn path is switched off
        transport.device("emulator-5554").shell("echo hi")
    assert spawn_trap == []


def test_local_only_operations_refuse_non_local_endpoints(host_agent, spawn_trap):
    for endpoint in (
        AdbEndpoint.create("192.0.2.7", 5555),
        AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1"),
        AdbEndpoint.create("127.0.0.1", 15037),
    ):
        transport = EndpointTransport(endpoint, adb_path="adb-sentinel")
        with pytest.raises(LocalOnlyOperation):
            transport.start_server()
        with pytest.raises(LocalOnlyOperation):
            transport.kill_server()
    assert spawn_trap == []


def test_local_default_transport_may_start_the_local_server(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "artemis.runtime.adb_endpoint.subprocess.run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )

    EndpointTransport(AdbEndpoint.local(), adb_path="adb-sentinel").start_server()

    assert calls == [["adb-sentinel", "-H", "127.0.0.1", "-P", "5037", "start-server"]]
