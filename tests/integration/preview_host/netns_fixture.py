"""Firewall negative-test topology, run as root inside a disposable container.

The container's own network namespace plays the host (H). Two preview namespaces
and an "internet" namespace hang off it on veth pairs, so the real kernel evaluates
the real ruleset without touching the actual host. Run with
`--cap-add NET_ADMIN --cap-add SYS_ADMIN` and forwarding sysctls (see the test).

  python netns_fixture.py            # prints JSON: control, enforced, ingress
  python netns_fixture.py serve P..  # TCP accept + UDP echo on each port (internal)
"""

import json
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/src")
from scripts import preview_sandbox as sb  # noqa: E402

PROBE = "/src/scripts/preview_isolation_probe.py"
SHA, IMAGE = "a" * 40, "sha256:" + "b" * 64


def sh(*cmd: str, ns: int | None = None, stdin: str | None = None) -> str:
    if ns is not None:
        cmd = ("nsenter", "-t", str(ns), "-n", *cmd)
    done = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if done.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {done.stderr.strip()}")
    return done.stdout


def serve(ports: list[int]) -> None:
    def tcp(port: int) -> None:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
        sock.listen()
        while True:
            sock.accept()[0].close()

    def udp(port: int) -> None:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
        while True:
            data, peer = sock.recvfrom(64)
            sock.sendto(data, peer)

    for port in ports:
        for fn in (tcp, udp):
            threading.Thread(target=fn, args=(port,), daemon=True).start()
    time.sleep(10**6)


def new_netns() -> int:
    proc = subprocess.Popen(["unshare", "-n", "sleep", "infinity"])
    time.sleep(0.2)
    return proc.pid


def attach_preview(slot: int) -> tuple[sb.Preview, int]:
    preview = sb.Preview(pr=100 + slot, head_sha=SHA, image_id=IMAGE, slot=slot)
    pid, peer = new_netns(), f"peer{slot}"
    v6 = f"fd00:24{slot}::"
    sh("ip", "link", "add", preview.bridge, "type", "veth", "peer", "name", peer)
    sh("ip", "link", "set", peer, "netns", str(pid))
    for cmd in (
        ("ip", "addr", "add", f"{preview.gateway}/28", "dev", preview.bridge),
        ("ip", "-6", "addr", "add", f"{v6}1/64", "dev", preview.bridge, "nodad"),
        ("ip", "link", "set", preview.bridge, "up"),
    ):
        sh(*cmd)
    for cmd in (
        ("ip", "link", "set", "lo", "up"),
        ("ip", "link", "set", peer, "name", "eth0"),
        ("ip", "addr", "add", f"{preview.ip}/28", "dev", "eth0"),
        ("ip", "-6", "addr", "add", f"{v6}2/64", "dev", "eth0", "nodad"),
        ("ip", "link", "set", "eth0", "up"),
        ("ip", "route", "add", "default", "via", preview.gateway),
        ("ip", "-6", "route", "add", "default", "via", f"{v6}1"),
    ):
        sh(*cmd, ns=pid)
    return preview, pid


def attach_internet() -> int:
    pid = new_netns()
    sh("ip", "link", "add", "hx0", "type", "veth", "peer", "name", "peerx")
    sh("ip", "link", "set", "peerx", "netns", str(pid))
    for cmd in (
        ("ip", "addr", "add", "203.0.113.1/30", "dev", "hx0"),
        ("ip", "-6", "addr", "add", "2001:db8:ffff::1/64", "dev", "hx0", "nodad"),
        ("ip", "link", "set", "hx0", "up"),
        ("ip", "route", "add", "198.51.100.7/32", "via", "203.0.113.2"),
        ("ip", "-6", "route", "add", "2001:db8::7/128", "via", "2001:db8:ffff::2"),
    ):
        sh(*cmd)
    for cmd in (
        ("ip", "link", "set", "lo", "up"),
        ("ip", "link", "set", "peerx", "name", "eth0"),
        ("ip", "addr", "add", "203.0.113.2/30", "dev", "eth0"),
        ("ip", "-6", "addr", "add", "2001:db8:ffff::2/64", "dev", "eth0", "nodad"),
        ("ip", "addr", "add", "198.51.100.7/32", "dev", "lo"),
        ("ip", "-6", "addr", "add", "2001:db8::7/128", "dev", "lo", "nodad"),
        ("ip", "link", "set", "eth0", "up"),
        # src hints: a connected UDP probe drops replies from any other source address
        ("ip", "route", "add", "default", "via", "203.0.113.1", "src", "198.51.100.7"),
        ("ip", "-6", "route", "add", "default", "via", "2001:db8:ffff::1", "src", "2001:db8::7"),
    ):
        sh(*cmd, ns=pid)
    return pid


def host_addresses() -> None:
    sh("ip", "link", "add", "dummy0", "type", "dummy")
    sh("ip", "link", "set", "dummy0", "up")
    for addr in ("10.1.2.3", "192.168.5.5", "172.16.9.9", "100.64.1.1", "100.100.100.100"):
        sh("ip", "addr", "add", f"{addr}/32", "dev", "dummy0")
    sh("ip", "addr", "add", "169.254.169.254/32", "dev", "dummy0")
    sh("ip", "-6", "addr", "add", "fd7a:115c:a1e0::1/128", "dev", "dummy0", "nodad")


def spawn_serve(*ports: int, ns: int | None = None) -> None:
    cmd = ["python3", __file__, "serve", *map(str, ports)]
    if ns is not None:
        cmd = ["nsenter", "-t", str(ns), "-n", *cmd]
    subprocess.Popen(cmd)


def run_probe(targets: dict[str, str], ns: int | None) -> dict[str, str]:
    cmd = ["python3", PROBE, "--timeout", "2", *targets.values()]
    if ns is not None:
        cmd = ["nsenter", "-t", str(ns), "-n", *cmd]
    out = json.loads(subprocess.run(cmd, text=True, capture_output=True).stdout)
    return {name: out[url] for name, url in targets.items()}


def main() -> None:
    host_addresses()
    internet = attach_internet()
    pv0, ns0 = attach_preview(0)
    pv1, ns1 = attach_preview(1)
    spawn_serve(9000, 53)
    spawn_serve(9000, 53, ns=internet)
    spawn_serve(8080, ns=ns0)
    spawn_serve(8080, ns=ns1)
    time.sleep(1)
    targets = {
        "host_gateway": f"tcp://{pv0.gateway}:9000",
        "host_gateway_dns_udp": f"udp://{pv0.gateway}:53",
        "private_10": "tcp://10.1.2.3:9000",
        "private_192_168": "tcp://192.168.5.5:9000",
        "private_172_16": "tcp://172.16.9.9:9000",
        "tailnet_cgnat": "tcp://100.64.1.1:9000",
        "tailnet_magicdns": "tcp://100.100.100.100:9000",
        "metadata": "tcp://169.254.169.254:9000",
        "ipv6_host_ula": "tcp://[fd7a:115c:a1e0::1]:9000",
        "ipv6_host_gateway": "tcp://[fd00:240::1]:9000",
        "ipv6_forwarded": "tcp://[2001:db8::7]:9000",
        "internet_v4": "tcp://198.51.100.7:9000",
        "internet_dns_udp": "udp://198.51.100.7:53",
        "other_preview": f"tcp://{pv1.ip}:8080",
    }
    control = run_probe(targets, ns0)  # no rules yet: every target must be reachable
    sh("nft", "-f", "-", stdin=sb.FIREWALL_RULESET)
    enforced = run_probe(targets, ns0)
    ingress = run_probe({"preview_app": f"tcp://{pv0.ip}:8080"}, None)["preview_app"]
    print(json.dumps({"control": control, "enforced": enforced, "ingress": ingress}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve([int(p) for p in sys.argv[2:]])
    else:
        main()
