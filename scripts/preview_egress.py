"""Validate preview dependency destinations without resolving names or opening sockets."""

from collections.abc import Iterable, Mapping
from ipaddress import IPv4Network, IPv6Network, ip_address
from urllib.parse import urlsplit

ALLOWED_HOSTS: tuple[str, ...] = ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org")
HTTPS_PORT = 443
MAX_HOPS = 5
HOSTS_PIN_PATH = "/run/artemis-preview/egress.hosts"

DENIED_IPV4 = tuple(
    IPv4Network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
IPV6_GLOBAL_UNICAST = IPv6Network("2000::/3")
DENIED_GLOBAL_UNICAST_IPV6 = tuple(
    IPv6Network(cidr) for cidr in ("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
)
SQUID_DENIED_IPV6 = (
    "::1/128",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "fc00::/7",
    "fe80::/10",
    "fec0::/10",
    "ff00::/8",
)


def _check_authority(host: str, port: int) -> None:
    if not host.isascii() or host.removesuffix(".").lower() not in ALLOWED_HOSTS:
        raise ValueError("destination hostname is not approved")
    if type(port) is not int or port != HTTPS_PORT:
        raise ValueError("destination must use port 443")


def check_destination(host: str, port: int, resolved: Iterable[str]) -> None:
    """Raise ValueError unless the authority and every address in the answer are approved."""
    _check_authority(host, port)
    found_address = False
    for text in resolved:
        if not isinstance(text, str) or "%" in text:
            raise ValueError("destination answer must contain unscoped IP address strings")
        address = ip_address(text)
        if address.version == 4:
            denied = any(address in network for network in DENIED_IPV4)
        else:
            denied = address not in IPV6_GLOBAL_UNICAST or any(
                address in network for network in DENIED_GLOBAL_UNICAST_IPV6
            )
        if denied:
            raise ValueError("destination resolved to a denied address")
        found_address = True
    if not found_address:
        raise ValueError("destination has no resolved addresses")


def check_redirect_chain(urls: Iterable[str]) -> None:
    """Check ordered, absolute hop URLs; validate their resolved addresses separately."""
    for hop, url in enumerate(urls, start=1):
        if hop > MAX_HOPS:
            raise ValueError("redirect chain exceeds five hops")
        if any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError("redirect URL contains whitespace or control characters")
        target = urlsplit(url)
        if target.scheme != "https" or not target.hostname or target.username is not None:
            raise ValueError("redirect must use HTTPS without user information")
        port = target.port
        if target.netloc.endswith(":"):
            raise ValueError("redirect has an empty port")
        _check_authority(target.hostname, HTTPS_PORT if port is None else port)


def render_hosts_pin(answers: Mapping[str, Iterable[str]]) -> str:
    """Validate complete trusted A/AAAA results and pin one address per Squid cache key.

    Squid replaces repeated hosts-file entries for a hostname. Pick a single
    address deterministically, preferring IPv4 only after checking both families.
    """
    if set(answers) != set(ALLOWED_HOSTS):
        raise ValueError("pin requires exactly the approved registry hosts")
    lines = []
    for host in ALLOWED_HOSTS:
        answer = tuple(answers[host])
        check_destination(host, HTTPS_PORT, answer)
        address = min(map(ip_address, answer), key=lambda item: (item.version, int(item)))
        lines.append(f"{address} {host}\n")
    return "".join(lines)


def render_proxy_config(listen_address: str = "127.0.0.1") -> str:
    """Render standalone Squid config for a fresh instance with no local DNS listener."""
    address = ip_address(listen_address)
    if not address.is_loopback or "%" in listen_address:
        raise ValueError("proxy must listen on an unscoped loopback address")
    bind_host = f"[{address}]" if address.version == 6 else str(address)
    denied_ipv4 = " ".join(map(str, DENIED_IPV4))
    denied_ipv6 = " ".join((*map(str, DENIED_GLOBAL_UNICAST_IPV6), *SQUID_DENIED_IPV6))
    return f"""http_port {bind_host}:3128
hosts_file {HOSTS_PIN_PATH}
dns_nameservers 127.0.0.1
dns_multicast_local off
cache deny all
forwarded_for delete

acl preview_connect method CONNECT
acl preview_https port {HTTPS_PORT}
acl preview_registry dstdomain -n {" ".join(ALLOWED_HOSTS)}
acl preview_denied_ipv4 dst {denied_ipv4}
acl preview_denied_ipv6 dst {denied_ipv6}
acl preview_resolved dst 0.0.0.0/0 {IPV6_GLOBAL_UNICAST}

http_access deny !preview_connect
http_access deny !preview_https
http_access deny !preview_registry
http_access deny preview_denied_ipv4
http_access deny preview_denied_ipv6
http_access allow preview_resolved
http_access deny all
"""
