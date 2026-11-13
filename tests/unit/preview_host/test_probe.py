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


def _raise(exc):
    def fail(*_):
        raise exc

    return fail


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (socket.EAI_NONAME, "reachable"),  # NXDOMAIN: the resolver answered
        (socket.EAI_AGAIN, "blocked"),  # nobody answered in time
        (socket.EAI_FAIL, "unknown"),  # never counts as isolation
    ],
)
def test_dns_resolution_failures_are_classified_by_whether_anyone_answered(
    monkeypatch, error, expected
):
    monkeypatch.setattr(socket, "getaddrinfo", _raise(socket.gaierror(error, "x")))
    assert probe.probe("dns://example.com", 0.1) == expected


def test_resolved_name_is_reachable(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [("ok",)])
    assert probe.probe("dns://example.com", 0.1) == "reachable"


@pytest.fixture
def dns_server():
    """UDP DNS server: ignores anything that is not a DNS query, NXDOMAINs the rest."""
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(3)
        state = {"queries": 0}

        def serve():
            try:
                while True:
                    data, peer = sock.recvfrom(512)
                    if len(data) >= 12 and data[2:4] == b"\x01\x00":
                        state["queries"] += 1
                        sock.sendto(data[:2] + b"\x81\x83" + data[4:], peer)
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        yield sock.getsockname()[1], state


def test_valid_dns_query_gets_nxdomain_and_that_is_reachable(dns_server):
    port, state = dns_server
    assert probe.probe(f"dns-udp://127.0.0.1:{port}", 1) == "reachable"
    assert state["queries"] == 1


def test_generic_udp_payload_misses_a_dns_server_which_is_why_dns_needs_dns_udp(dns_server):
    port, _ = dns_server
    assert probe.probe(f"udp://127.0.0.1:{port}", 0.3) == "blocked"
    assert probe.probe(f"dns-udp://127.0.0.1:{port}", 1) == "reachable"


def test_dns_udp_timeout_is_blocked_and_a_foreign_reply_is_unknown(monkeypatch):
    monkeypatch.setattr(socket.socket, "recv", _raise(TimeoutError()))
    assert probe.probe("dns-udp://10.0.0.1:53", 0.1) == "blocked"
    monkeypatch.setattr(socket.socket, "recv", lambda *_: b"\x00\x00garbage")
    monkeypatch.setattr(socket.socket, "connect", lambda *_: None)
    monkeypatch.setattr(socket.socket, "send", lambda *_: 0)
    monkeypatch.setattr(probe.secrets, "randbits", lambda _: 0x1234)
    assert probe.probe("dns-udp://10.0.0.1:53", 0.1) == "unknown"


def test_dns_query_is_a_valid_recursive_a_query():
    qid, wire = probe.dns_query("example.com")
    assert wire[:2] == qid.to_bytes(2, "big")
    assert wire[2:4] == b"\x01\x00" and wire[4:6] == b"\x00\x01"
    assert wire[12:] == b"\x07example\x03com\x00\x00\x01\x00\x01"


def test_exit_code_requires_every_target_blocked(tcp_listener, monkeypatch, capsys):
    assert probe.main([f"tcp://127.0.0.1:{tcp_listener}"]) == 1
    monkeypatch.setattr(probe, "probe", lambda *_: "blocked")
    assert probe.main(["tcp://10.0.0.1:80", "dns://x"]) == 0
    assert '"blocked"' in capsys.readouterr().out


def test_unsupported_target_is_an_error():
    with pytest.raises(ValueError):
        probe.probe("http://example.com", 1)
