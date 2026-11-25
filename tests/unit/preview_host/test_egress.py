import os
import subprocess
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
    assert "acl preview_resolved dst 0.0.0.0/0 2000::/3" in lines
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


def test_squid_applies_connect_acl_to_both_pin_families(tmp_path):
    image = os.environ.get("ARTEMIS_TEST_SQUID_IMAGE")
    if not image:
        pytest.skip("set ARTEMIS_TEST_SQUID_IMAGE to a locally available Squid image")

    tmp_path.chmod(0o755)
    (tmp_path / "egress.hosts").write_text(
        f"{PUBLIC_IPV4} registry.npmjs.org\n{PUBLIC_IPV6} files.pythonhosted.org\n"
        "10.0.0.5 pypi.org\n",
        encoding="utf-8",
    )
    (tmp_path / "squid.conf").write_text(
        egress.render_proxy_config().replace(egress.HOSTS_PIN_PATH, "/fixtures/egress.hosts")
        + "pid_filename /tmp/squid.pid\ncache_log /tmp/squid.log\naccess_log none\n"
        "connect_timeout 2 seconds\n",
        encoding="utf-8",
    )
    cases = {
        "registry.npmjs.org:443": "503",
        "files.pythonhosted.org:443": "503",
        "pypi.org:443": "403",
        "registry.npmjs.org:8443": "403",
        "example.test:443": "403",
    }
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--user=65534:65534",
            "--cap-drop=ALL",
            "--mount",
            f"type=bind,source={tmp_path},target=/fixtures,readonly",
            "--entrypoint=/bin/bash",
            image,
            "-euc",
            r"""
            squid -N -f /fixtures/squid.conf >&2 &
            for attempt in {1..100}; do
                if (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null; then break; fi
                sleep .05
            done
            for authority in "$@"; do
                exec 3<>/dev/tcp/127.0.0.1/3128
                printf 'CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' "$authority" "$authority" >&3
                read -r protocol status reason <&3
                printf '%s %s\n' "$authority" "$status"
                exec 3>&-
            done
            """,
            "squid-connect",
            *cases,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{authority} {status}" for authority, status in cases.items()
    ]


ROOT = Path(__file__).resolve().parents[3]
PROXY_UID = 987


def _chain(ruleset, name):
    """Return the stripped rule lines of one chain (hermetic text parse; no nft)."""
    block = ruleset.split(f"chain {name} {{\n", 1)[1].split("\n  }", 1)[0]
    return [line.strip() for line in block.splitlines()]


def test_acquisition_ruleset_is_one_inet_table_with_policy_drop():
    ruleset = egress.render_acquisition_ruleset(PROXY_UID)
    assert ruleset.count("table ") == 1
    assert ruleset.startswith("table inet artemis_preview_egress {\n")
    assert ruleset.count("policy drop;") == 2  # input and output
    assert "policy accept" not in ruleset
    assert "hook output" in ruleset and "hook input" in ruleset


def test_non_proxy_uid_cannot_open_tcp_443_udp_or_dns():
    ruleset = egress.render_acquisition_ruleset(PROXY_UID)
    accepts = [line for line in _chain(ruleset, "output") if line.endswith("accept")]
    assert accepts == [
        "ct state established,related accept",
        'oifname "lo" tcp dport 3128 ct state new accept',
        f"meta skuid {PROXY_UID} meta nfproto ipv4 tcp dport 443 ct state new accept",
        f"meta skuid {PROXY_UID} ip6 daddr 2000::/3 tcp dport 443 ct state new accept",
    ]
    # Every route to 443 carries the proxy UID; nothing allows UDP or port 53.
    assert all(f"skuid {PROXY_UID}" in line for line in accepts if "dport 443" in line)
    assert "udp" not in ruleset
    assert "dport 53" not in ruleset
    assert "dport 53" not in "".join(_chain(ruleset, "input"))


def test_proxy_uid_denied_ranges_are_dropped_before_any_443_accept():
    ruleset = egress.render_acquisition_ruleset(PROXY_UID)
    rules = _chain(ruleset, "output")
    first_443 = next(i for i, line in enumerate(rules) if "dport 443" in line)
    drops = rules[:first_443]
    v4 = next(line for line in drops if line.startswith(f"meta skuid {PROXY_UID} ip daddr {{"))
    v6 = next(line for line in drops if line.startswith(f"meta skuid {PROXY_UID} ip6 daddr {{"))
    assert v4.endswith("drop") and v6.endswith("drop")
    for network in egress.DENIED_IPV4:
        assert str(network) in v4
    for network in (*egress.DENIED_GLOBAL_UNICAST_IPV6, *egress.SQUID_DENIED_IPV6):
        assert str(network) in v6
    for form in ("::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "::/128", "::1/128"):
        assert form in v6  # IPv4-mapped, both NAT64 prefixes, unspecified, loopback


def test_denied_sets_do_not_overlap_because_nft_rejects_overlapping_elements():
    for networks in (
        egress.DENIED_IPV4,
        egress.ACQUISITION_DENIED_IPV6,
    ):
        for index, network in enumerate(networks):
            assert not any(network.overlaps(other) for other in networks[index + 1 :])


def test_denied_ipv6_covers_everything_outside_global_unicast_or_is_not_accepted():
    # The IPv6 accept rule only matches 2000::/3, so every denied range outside it
    # (mapped, NAT64, ULA, link-local) has no accept path even without the drop rule.
    outside = [
        n for n in egress.ACQUISITION_DENIED_IPV6 if not n.subnet_of(egress.IPV6_GLOBAL_UNICAST)
    ]
    assert {str(n) for n in outside} >= {
        "::ffff:0.0.0.0/96",
        "64:ff9b::/96",
        "fc00::/7",
        "fe80::/10",
    }


def test_loopback_is_allowed_only_to_the_proxy_port():
    ruleset = egress.render_acquisition_ruleset(PROXY_UID)
    assert [line for line in _chain(ruleset, "input") if line.endswith("accept")] == [
        "ct state established,related accept",
        'iifname "lo" tcp dport 3128 ct state new accept',
    ]
    assert ruleset.count('"lo"') == 2
    assert "3128" not in ruleset.replace("tcp dport 3128", "")  # no other port literal use


@pytest.mark.parametrize("uid", [0, -1, True, False, 2**32 - 1, 2**32, "987", 987.0, None])
def test_invalid_proxy_uid_is_refused(uid):
    with pytest.raises(ValueError):
        egress.render_acquisition_ruleset(uid)


def test_ruleset_uid_is_rendered_only_as_an_integer():
    assert "skuid 65534 " in egress.render_acquisition_ruleset(65534)


def test_checked_in_acquisition_ruleset_matches_the_renderer():
    checked_in = ROOT / "config/preview/egress-acquisition.nft"
    assert checked_in.read_text(encoding="utf-8") == egress.render_acquisition_ruleset(
        egress.PROXY_UID_PLACEHOLDER
    )


def test_proxy_unit_is_a_locked_down_squid_with_the_checked_in_config():
    unit = (ROOT / "config/preview/artemis-preview-egress.service").read_text(encoding="utf-8")
    lines = unit.splitlines()
    assert "User=artemis-preview-egress" in lines
    assert "Group=artemis-preview-egress" in lines
    assert "NoNewPrivileges=true" in lines
    assert "CapabilityBoundingSet=" in lines
    assert "AmbientCapabilities=" in lines
    assert "ProtectSystem=strict" in lines
    assert "ProtectHome=true" in lines
    assert "PrivateTmp=true" in lines
    assert "RuntimeDirectory=artemis-preview-egress" in lines
    # the runtime directory is the only writable path
    writable_keys = (
        "ReadWritePaths=",
        "ReadWriteDirectories=",
        "LogsDirectory=",
        "StateDirectory=",
    )
    writable_keys += (
        "CacheDirectory=",
        "ConfigurationDirectory=",
        "BindPaths=",
        "TemporaryFileSystem=",
    )
    assert not [line for line in lines if line.startswith(writable_keys)]
    assert "WantedBy=" not in unit  # file only: never enabled by itself
    exec_start = next(line for line in lines if line.startswith("ExecStart="))
    assert exec_start == "ExecStart=/usr/sbin/squid -N -f /etc/artemis-preview/egress-squid.conf"
    assert not any(line.startswith("ExecStartPre=+") for line in lines)  # no root helper


def test_proxy_account_is_a_no_login_system_user():
    sysusers = (ROOT / "config/preview/sysusers.conf").read_text(encoding="utf-8")
    assert (
        'u artemis-preview-egress - "Artemis preview egress proxy" /nonexistent /usr/sbin/nologin'
        in (sysusers.splitlines())
    )


def test_egress_doc_is_linked_and_covers_flow_bypass_rollback_and_risk():
    build = (ROOT / "docs/preview-build.md").read_text(encoding="utf-8")
    assert "preview-egress.md" in build
    doc = (ROOT / "docs/preview-egress.md").read_text(encoding="utf-8")
    for heading in ("## Acquisition flow", "## Bypass tests", "## Rollback", "## Remaining risk"):
        assert heading in doc
