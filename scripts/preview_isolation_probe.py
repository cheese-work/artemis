"""Egress probe run inside a preview's network namespace (CHE-1291, L4a).

Each target must be unreachable. `reachable` means a connect succeeded or the far
side answered (even a refusal or an ICMP error): the packet got through. Only a
timeout or a local routing/permission error counts as `blocked`; any other error is
`unknown` and fails the run, so a surprise never reads as isolation. Stdlib only.

Targets:
  tcp://HOST:PORT      connect.
  udp://HOST:PORT      generic datagram; reachable only on a reply or ICMP error. A
                       service that ignores the payload looks blocked, so never use it for DNS.
  dns-udp://HOST:PORT  protocol-valid DNS query; any answer with the query ID, including
                       NXDOMAIN or SERVFAIL, means the resolver is reachable.
  dns://NAME           system resolver. A resolved name or a negative answer (NXDOMAIN, no
                       data) is an answer and fails. Only a temporary failure counts as
                       blocked, so use a name that exists. A resolver that answers SERVFAIL
                       looks the same, which is why dns-udp targets are the stronger check.
Exit 0 only when every target is blocked.
"""

import argparse
import errno
import json
import secrets
import socket
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

BLOCKED_ERRNOS = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.EACCES,
    errno.EPERM,
    errno.EAFNOSUPPORT,  # IPv6 disabled in the namespace
    errno.EADDRNOTAVAIL,
}
ANSWERED_ERRNOS = {errno.ECONNREFUSED, errno.ECONNRESET}  # the far side replied


def _address(host: str) -> tuple[int, str]:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return family, host


def _classify(exc: OSError | None) -> str:
    if exc is None or exc.errno in ANSWERED_ERRNOS:
        return "reachable"
    if isinstance(exc, TimeoutError) or exc.errno in BLOCKED_ERRNOS:
        return "blocked"
    return "unknown"


def probe_stream(host: str, port: int, timeout: float) -> str:
    family, host = _address(host)
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
    except OSError as exc:
        return _classify(exc)
    return "reachable"


def probe_datagram(host: str, port: int, timeout: float) -> str:
    family, host = _address(host)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(b"probe")
            sock.recv(64)
    except OSError as exc:
        return _classify(exc)
    return "reachable"


def dns_query(name: str = "example.com") -> tuple[int, bytes]:
    """A recursive A query for `name`: (query ID, wire bytes)."""
    qid = secrets.randbits(16)
    labels = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    return qid, struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack(
        "!HH", 1, 1
    )


def probe_dns_datagram(host: str, port: int, timeout: float) -> str:
    family, host = _address(host)
    qid, query = dns_query()
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.send(query)
            reply = sock.recv(512)
    except OSError as exc:
        return _classify(exc)
    return "reachable" if reply[:2] == struct.pack("!H", qid) else "unknown"


def probe_dns(name: str, timeout: float) -> str:
    outcome: list[str] = []

    def resolve() -> None:
        try:
            socket.getaddrinfo(name, None)
            outcome.append("reachable")
        except socket.gaierror as exc:
            if exc.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)):
                outcome.append("reachable")  # a negative answer is still an answer
            elif exc.errno == socket.EAI_AGAIN:
                outcome.append("blocked")
            else:
                outcome.append("unknown")

    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(timeout * 4)  # the resolver retries; a hang is a drop
    return outcome[0] if outcome else "blocked"


def probe(target: str, timeout: float) -> str:
    url = urlsplit(target)
    if url.scheme == "dns":
        return probe_dns(url.netloc, timeout)
    runners = {"tcp": probe_stream, "udp": probe_datagram, "dns-udp": probe_dns_datagram}
    if url.scheme in runners and url.hostname and url.port:
        return runners[url.scheme](url.hostname, url.port, timeout)
    raise ValueError(f"unsupported target: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=1.0)
    parser.add_argument("targets", nargs="+")
    args = parser.parse_args(argv)
    with ThreadPoolExecutor(max_workers=len(args.targets)) as pool:
        results = dict(zip(args.targets, pool.map(lambda t: probe(t, args.timeout), args.targets)))
    print(json.dumps(results, sort_keys=True))
    return 0 if all(v == "blocked" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
