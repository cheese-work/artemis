"""A real preview container started from the sandbox argv (CHE-1291).

Build the image first: `docker build --target preview -t artemis-preview:l4a .`
(override with PREVIEW_IMAGE). Creates one disposable network and container with
exact names and removes them afterwards. The host firewall is NOT installed here
(that needs privileges), so reachability of the host is covered by test_firewall.py;
this module checks what the container configuration itself guarantees.
"""

import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

from scripts import preview_sandbox as sb
from tests.integration.preview_host.owned import Owned

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed"),
]

ROOT = Path(__file__).resolve().parents[3]
TEAM = "example.cloudflareaccess.com"


def docker(*args: str, stdin: str | None = None, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], input=stdin, text=True, capture_output=True)
    if check and done.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def b64url(number: int) -> str:
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def write_bundle(path: Path) -> None:
    numbers = rsa.generate_private_key(65537, 2048).public_key().public_numbers()
    key = {"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig"}
    key |= {"n": b64url(numbers.n), "e": b64url(numbers.e)}
    doc = {"issuer": f"https://{TEAM}", "fetched_at": int(time.time()), "keys": [key]}
    path.write_text(json.dumps(doc))
    path.chmod(0o644)


ENV = {
    "ARTEMIS_AUTH_MODE": "cloudflare",
    "ARTEMIS_CF_ACCESS_TEAM_DOMAIN": TEAM,
    "ARTEMIS_CF_ACCESS_AUD": "test-aud",
    "ARTEMIS_PREVIEW_QA_EMAILS": "qa1@example.com,qa2@example.com",
    "ARTEMIS_ADMIN_EMAILS": "admin@example.com",
}


def unique_preview(image_id: str, slot: int = 0) -> sb.Preview:
    """A PR number no other run uses, so container and network names are exclusive."""
    pr = 10**9 + secrets.randbelow(10**9)
    return sb.Preview(pr=pr, head_sha="c" * 40, image_id=image_id, slot=slot)


@pytest.fixture(scope="module")
def image_tag() -> str:
    return os.environ.get("PREVIEW_IMAGE", "artemis-preview:l4a")


@pytest.fixture(scope="module")
def image_id(image_tag) -> str:
    found = docker("image", "inspect", "-f", "{{.Id}}", image_tag, check=False)
    if not found.startswith("sha256:"):
        pytest.skip(f"preview image {image_tag} not built")
    return found


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("jwks") / "jwks.json"
    write_bundle(path)
    return path


@pytest.fixture(scope="module")
def container(image_id, bundle):
    owned = Owned(docker)
    preview = unique_preview(image_id)
    try:
        sb.verify_image(json.loads(docker("image", "inspect", image_id))[0])
        owned.network(sb.network_create_argv(preview))
        owned.container(sb.container_create_argv(preview, ENV, str(bundle)))
        sb.verify_container(inspect(preview))
        docker("start", preview.name)
        yield preview
    finally:
        owned.cleanup()  # only the IDs created above


def inspect(preview: sb.Preview) -> dict:
    return json.loads(docker("inspect", preview.name))[0]


def run_in(preview: sb.Preview, code: str, *args: str) -> str:
    return docker("exec", "-i", preview.name, "python", "-", *args, stdin=code, check=False)


def test_runtime_options_are_applied_by_the_engine(container):
    info = inspect(container)
    host = info["HostConfig"]
    assert info["Config"]["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"] and not host["CapAdd"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["PidsLimit"] == 256
    assert host["Memory"] == host["MemorySwap"] == 1024**3
    assert host["NanoCpus"] == 10**9
    assert host["NetworkMode"] == container.name
    assert host["PidMode"] == "" and host["IpcMode"] == "private"
    assert host["PortBindings"] == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18100"}]}
    assert [(m["Type"], m["Destination"], m["RW"]) for m in info["Mounts"]] == [
        ("bind", sb.JWKS_PATH, False)
    ]


def test_app_serves_in_the_preview_profile_and_rejects_unsigned_requests(container):
    deadline, status = time.time() + 60, None
    while status is None and time.time() < deadline:
        try:
            status = urllib.request.urlopen("http://127.0.0.1:18100/api/runs", timeout=3).status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except OSError:
            time.sleep(1)
    assert status in (401, 403), f"expected an Access rejection, got {status}"


def test_image_has_no_adb_and_filesystem_is_read_only_for_non_root(container):
    code = (
        "import os, shutil, tempfile\n"
        "print(os.getuid(), shutil.which('adb'), shutil.which('git'), shutil.which('curl'))\n"
        "try:\n    open('/app/x', 'w')\nexcept OSError as e:\n    print('ro', e.errno)\n"
        "open('/tmp/x', 'w').close(); print('tmp-ok')\n"
    )
    assert run_in(container, code).split("\n") == ["10001 None None None", "ro 30", "tmp-ok"]


def test_ipv6_is_disabled_inside_the_container(container):
    probe = (ROOT / "scripts" / "preview_isolation_probe.py").read_text()
    out = run_in(container, probe, "tcp://[fd00::1]:80", "tcp://[2001:db8::7]:80")
    assert json.loads(out) == {
        "tcp://[fd00::1]:80": "blocked",
        "tcp://[2001:db8::7]:80": "blocked",
    }


def test_image_declared_volume_is_refused_before_create_and_caught_after(
    image_tag, image_id, bundle
):
    """A hostile image declaring VOLUME gets writable host-backed storage despite --read-only."""
    owned = Owned(docker)
    try:
        tag = f"artemis-preview-hostile:{secrets.token_hex(4)}"
        hostile_id = owned.image(tag, f"FROM {image_tag}\nVOLUME /scratch\n")
        with pytest.raises(ValueError, match="/scratch"):
            sb.verify_image(json.loads(docker("image", "inspect", hostile_id))[0])
        preview = unique_preview(hostile_id, slot=1)
        owned.network(sb.network_create_argv(preview))
        owned.container(sb.container_create_argv(preview, ENV, str(bundle)))
        assert any(m["Destination"] == "/scratch" for m in inspect(preview)["Mounts"])
        with pytest.raises(ValueError, match="unexpected mounts"):
            sb.verify_container(inspect(preview))
    finally:
        owned.cleanup()


def test_image_cleanup_keeps_other_tags_that_share_the_image_id(image_tag, image_id):
    """`FROM <tag>` alone builds the same image ID, so a second tag shares it."""
    owned = Owned(docker)
    try:
        shared = owned.image(
            f"artemis-preview-shared:{secrets.token_hex(4)}", f"FROM {image_tag}\n"
        )
        assert shared == image_id
    finally:
        owned.cleanup()
    assert docker("image", "inspect", "-f", "{{.Id}}", image_tag) == image_id  # untouched


def test_non_loopback_publish_is_not_reachable_on_other_host_addresses(container):
    host_ip = docker(
        "network", "inspect", container.name, "-f", "{{(index .IPAM.Config 0).Gateway}}"
    )
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://{host_ip}:18100/", timeout=2)
