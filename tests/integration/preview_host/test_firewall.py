"""The preview firewall ruleset against a real kernel, in a disposable container (CHE-1291).

The container is the "host"; preview and internet namespaces hang off it. No rule,
route or interface is created on the real host. Needs docker; run with
`pytest -m integration tests/integration/preview_host/test_firewall.py`.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed"),
]

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
IMAGE = "artemis-preview-netfixture:l4a"


@pytest.fixture(scope="module")
def result() -> dict:
    subprocess.run(
        [
            "docker",
            "build",
            "-q",
            "-t",
            IMAGE,
            "-f",
            str(HERE / "Dockerfile.netfixture"),
            str(HERE),
        ],
        check=True,
        capture_output=True,
    )
    done = subprocess.run(
        [
            "docker", "run", "--rm",
            "--cap-add", "NET_ADMIN", "--cap-add", "SYS_ADMIN",
            "--sysctl", "net.ipv4.ip_forward=1",
            "--sysctl", "net.ipv6.conf.all.forwarding=1",
            "-v", f"{ROOT}:/src:ro",
            IMAGE, "python3", "/src/tests/integration/preview_host/netns_fixture.py",
        ],
        text=True,
        capture_output=True,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_probe_is_sensitive_without_rules(result):
    """Control: with no ruleset every target answers, so each later block is meaningful."""
    assert {k: v for k, v in result["control"].items() if v != "reachable"} == {}


def test_every_target_is_blocked_with_the_ruleset(result):
    assert {k: v for k, v in result["enforced"].items() if v != "blocked"} == {}


def test_ingress_to_the_preview_still_works(result):
    """Replies to host-initiated connections (Traefik, loopback publish) pass."""
    assert result["ingress"] == "reachable"
