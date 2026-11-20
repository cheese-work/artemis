from pathlib import Path

import pytest

from scripts import preview_egress as egress


PUBLIC_IPV4 = "104.16.24.34"
PUBLIC_IPV6 = "2606:4700::6810:1822"


@pytest.mark.parametrize(
    "host",
    ["registry.npmjs.org", "REGISTRY.NPMJS.ORG.", "pypi.org", "files.pythonhosted.org"],
)
def test_public_destination_is_allowed(host):
    egress.check_destination(host, 443, [PUBLIC_IPV4, PUBLIC_IPV6])


@pytest.mark.parametrize(
    "host",
    [
        "user@registry.npmjs.org",
        "registry.npmjs.org.evil.test",
        "evil.test#registry.npmjs.org",
        "xregistry.npmjs.org",
        "registry.npmjs.org..",
        " registry.npmjs.org",
        "127.0.0.1",
        "2130706433",
        "0x7f.1",
        "0177.0.0.1",
        "[::1]",
        "[::ffff:127.0.0.1]",
    ],
)
def test_other_hosts_and_ip_literals_are_denied(host):
    with pytest.raises(ValueError):
        egress.check_destination(host, 443, [PUBLIC_IPV4])


@pytest.mark.parametrize(
    "denied",
    [
        "0.12.34.56",
        "10.1.2.3",
        "100.100.100.100",
        "127.0.0.1",
        "169.254.1.2",
        "169.254.169.254",
        "172.16.0.1",
        "172.31.240.1",
        "192.0.0.9",
        "192.0.2.1",
        "192.88.99.1",
        "192.168.1.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:104.16.24.34",
        "64:ff9b::6810:1822",
        "64:ff9b:1::1",
        "100::1",
        "2001:0:1234::1",
        "2001:db8::1",
        "2002:6810:1822::1",
        "3fff::1",
        "fc00::1",
        "fd00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
    ],
)
def test_one_denied_address_rejects_the_entire_answer(denied):
    for answer in ([denied], [PUBLIC_IPV4, denied, PUBLIC_IPV6], [denied, PUBLIC_IPV4]):
        with pytest.raises(ValueError):
            egress.check_destination("registry.npmjs.org", 443, iter(answer))
        answers = dict.fromkeys(egress.ALLOWED_HOSTS, [PUBLIC_IPV4])
        answers["registry.npmjs.org"] = answer
        with pytest.raises(ValueError, match="denied address"):
            egress.render_hosts_pin(answers)


@pytest.mark.parametrize(
    "answer", [[], ["not-an-address"], [1745885226], [PUBLIC_IPV4, "2606:4700::1%eth0"]]
)
def test_missing_or_invalid_answer_is_denied(answer):
    with pytest.raises(ValueError):
        egress.check_destination("registry.npmjs.org", 443, answer)


@pytest.mark.parametrize("port", [80, 8443])
def test_non_https_ports_are_denied(port):
    with pytest.raises(ValueError):
        egress.check_destination("registry.npmjs.org", port, [PUBLIC_IPV4])


def test_redirect_chain_accepts_only_up_to_five_approved_https_urls():
    egress.check_redirect_chain(
        ["https://pypi.org/simple/", "https://FILES.PYTHONHOSTED.ORG.:443/a"]
    )
    egress.check_redirect_chain(["https://registry.npmjs.org/pkg"] * 5)


@pytest.mark.parametrize(
    "target",
    [
        "http://pypi.org/simple/",
        "https://pypi.org.evil.test/",
        "https://pypi.org:8443/",
        "https://pypi.org:/",
        "https://user@pypi.org/",
        "https://[::1]/",
        "https://py\npi.org/",
        "//pypi.org/simple/",
    ],
)
def test_redirect_chain_checks_every_url(target):
    with pytest.raises(ValueError):
        egress.check_redirect_chain(["https://pypi.org/simple/", target])


def test_six_hops_are_denied():
    with pytest.raises(ValueError):
        egress.check_redirect_chain(["https://pypi.org/simple/"] * 6)


def test_pin_renders_one_checked_address_per_host():
    answers = {
        "files.pythonhosted.org": iter([PUBLIC_IPV6]),
        "pypi.org": iter(["104.16.24.35", PUBLIC_IPV4]),
        "registry.npmjs.org": iter([PUBLIC_IPV6, PUBLIC_IPV4]),
    }
    assert egress.render_hosts_pin(answers) == (
        "104.16.24.34 registry.npmjs.org\n"
        "104.16.24.34 pypi.org\n"
        "2606:4700::6810:1822 files.pythonhosted.org\n"
    )


@pytest.mark.parametrize("invalid_host", [None, "pypi.org\n127.0.0.1 files.pythonhosted.org"])
def test_pin_requires_exact_registry_hosts(invalid_host):
    answers = dict.fromkeys(egress.ALLOWED_HOSTS, [PUBLIC_IPV4])
    if invalid_host is not None:
        answers[invalid_host] = [PUBLIC_IPV4]
    else:
        del answers["pypi.org"]
    with pytest.raises(ValueError, match="exactly"):
        egress.render_hosts_pin(answers)


def test_pin_rejects_empty_answer():
    answers = dict.fromkeys(egress.ALLOWED_HOSTS, [PUBLIC_IPV4])
    answers["pypi.org"] = []
    with pytest.raises(ValueError, match="no resolved addresses"):
        egress.render_hosts_pin(answers)


def test_proxy_config_is_current_and_requires_a_resolved_registry_connect():
    rendered = egress.render_proxy_config()
    checked_in = Path(__file__).resolve().parents[3] / "config/preview/egress-squid.conf"
    assert checked_in.read_text(encoding="utf-8") == rendered
    lines = rendered.splitlines()
    assert "hosts_file /run/artemis-preview/egress.hosts" in lines
    assert "dns_nameservers 127.0.0.1" in lines
    assert "dns_multicast_local off" in lines
    assert "acl preview_connect method CONNECT" in lines
    assert "acl preview_https port 443" in lines
    assert (
        "acl preview_registry dstdomain -n registry.npmjs.org pypi.org files.pythonhosted.org"
        in lines
    )
    assert "acl preview_resolved dst ipv4 2000::/3" in lines
    assert [line for line in lines if line.startswith("http_access ")] == [
        "http_access deny !preview_connect",
        "http_access deny !preview_https",
        "http_access deny !preview_registry",
        "http_access deny preview_denied_ipv4",
        "http_access deny preview_denied_ipv6",
        "http_access allow preview_resolved",
        "http_access deny all",
    ]
    assert "cache deny all" in lines
    assert "cache_dir" not in rendered
    assert "forwarded_for delete" in lines
    assert "::ffff:0:0/96" not in rendered


def test_proxy_binds_only_to_a_scopeless_loopback():
    assert egress.render_proxy_config().splitlines()[0] == "http_port 127.0.0.1:3128"
    assert egress.render_proxy_config("::1").splitlines()[0] == "http_port [::1]:3128"
    for address in ("0.0.0.0", "::", "localhost", "::1%eth0", "127.0.0.1\nhttp_port 80"):
        with pytest.raises(ValueError):
            egress.render_proxy_config(address)
