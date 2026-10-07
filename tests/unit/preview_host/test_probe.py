"""The egress probe must tell a drop from an answer (CHE-1291)."""

import errno
import socket
import threading

import pytest

from scripts import preview_isolation_probe as probe


@pytest.fixture
def tcp_listener():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield sock.getsockname()[1]


@pytest.fixture
def udp_echo():
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2)
        port = sock.getsockname()[1]

        def serve():
            data, peer = sock.recvfrom(64)
            sock.sendto(data, peer)

        threading.Thread(target=serve, daemon=True).start()
        yield port


def test_open_tcp_port_is_reachable(tcp_listener):
    assert probe.probe(f"tcp://127.0.0.1:{tcp_listener}", 1) == "reachable"


def test_refused_tcp_port_is_reachable_because_the_host_answered():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert probe.probe(f"tcp://127.0.0.1:{port}", 1) == "reachable"


def test_udp_reply_is_reachable(udp_echo):
    assert probe.probe(f"udp://127.0.0.1:{udp_echo}", 1) == "reachable"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError(),
        OSError(errno.ENETUNREACH, "x"),
        OSError(errno.EHOSTUNREACH, "x"),
        OSError(errno.EPERM, "x"),
    ],
)
def test_drop_or_local_routing_error_is_blocked(monkeypatch, exc):
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(exc))
    assert probe.probe("tcp://10.0.0.1:80", 1) == "blocked"


def test_unrecognised_error_is_unknown_not_blocked(monkeypatch):
    err = OSError(errno.EIO, "x")
    monkeypatch.setattr(socket.socket, "connect", lambda *_: (_ for _ in ()).throw(err))
    assert probe.probe("tcp://10.0.0.1:80", 1) == "unknown"


def test_dns_failure_is_blocked_and_success_is_reachable(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: (_ for _ in ()).throw(socket.gaierror()))
    assert probe.probe("dns://example.com", 0.1) == "blocked"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [("ok",)])
    assert probe.probe("dns://example.com", 0.1) == "reachable"


def test_exit_code_requires_every_target_blocked(tcp_listener, monkeypatch, capsys):
    assert probe.main([f"tcp://127.0.0.1:{tcp_listener}"]) == 1
    monkeypatch.setattr(probe, "probe", lambda *_: "blocked")
    assert probe.main(["tcp://10.0.0.1:80", "dns://x"]) == 0
    assert '"blocked"' in capsys.readouterr().out


def test_unsupported_target_is_an_error():
    with pytest.raises(ValueError):
        probe.probe("http://example.com", 1)
