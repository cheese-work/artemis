"""Runtime sandbox for one per-PR preview container (CHE-1291, L4a).

Pure builders for the trusted host daemon: Docker network/container argv and the
host firewall ruleset. Nothing here runs Docker, writes a rule or touches the host;
the daemon (a later layer) executes the output. Runtime options come only from this
module, never from the image or the PR. Stdlib only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network
import re

MAX_SLOTS = 3
POOL = IPv4Network("172.31.240.0/24")  # one /28 per slot: .1 gateway, .2 preview
HOST_PORT_BASE = 18100  # loopback port = base + slot, Traefik's upstream
CONTAINER_PORT = 8080
UID = 10001
# Amends the planned 128 (recorded on CHE-1291). Measured on the L4a image: 106 tasks
# idle, 111 peak after boot. 93 of them are one `DataEngineHandler._drain_queue` thread
# per `get_logger` name, created at `import artemis`; they cannot be reduced without
# changing app code. 128 left 17 PIDs for the request thread pool.
PIDS_LIMIT = 256
BRIDGE_PREFIX = "artemis-pv"
STATE_DIR = "/var/lib/artemis-preview"
JWKS_PATH = "/run/preview/jwks.json"
# TEST-NET-1 blackhole: the embedded resolver dials custom upstreams from the
# container namespace, so the firewall drops them. Keeps DNS from using the host.
DNS_BLACKHOLE = "192.0.2.53"

# Caller-supplied configuration (docs/preview-fixtures.md); anything else is refused.
ALLOWED_ENV = frozenset(
    {
        "ARTEMIS_AUTH_MODE",
        "ARTEMIS_CF_ACCESS_TEAM_DOMAIN",
        "ARTEMIS_CF_ACCESS_AUD",
        "ARTEMIS_PREVIEW_QA_EMAILS",
        "ARTEMIS_ADMIN_EMAILS",
    }
)
_FIXED_ENV = {
    "ARTEMIS_PREVIEW_PROFILE": "1",
    "ARTEMIS_APP_DIR": STATE_DIR,
    "ARTEMIS_PREVIEW_JWKS_BUNDLE": JWKS_PATH,
    "TMPDIR": "/tmp",
    "HOME": "/tmp",
    "PYTHONDONTWRITEBYTECODE": "1",
    # Native thread pools default to one thread per host core and would exhaust the
    # PID limit at import time on a many-core host.
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
ENTRYPOINT = "python"
COMMAND = [
    "-m",
    "uvicorn",
    "apps.admin_console.server:app",
    "--host",
    "0.0.0.0",  # container-internal; published to loopback only
    "--port",
    str(CONTAINER_PORT),
]

# New connections from any preview bridge are dropped: host gateway and services,
# private/tailnet/metadata ranges, the internet and other previews. Replies to
# ingress (loopback publish, Traefik) stay allowed. `inet` covers IPv4 and IPv6.
# Priority -10 runs before Docker's own chains; a drop there is final.
FIREWALL_RULESET = f"""\
table inet artemis_preview {{
  chain input {{
    type filter hook input priority -10; policy accept;
    iifname "{BRIDGE_PREFIX}*" ct state established,related accept
    iifname "{BRIDGE_PREFIX}*" drop
  }}
  chain forward {{
    type filter hook forward priority -10; policy accept;
    iifname "{BRIDGE_PREFIX}*" ct state established,related accept
    iifname "{BRIDGE_PREFIX}*" drop
  }}
}}
"""

_SHA = re.compile(r"[0-9a-f]{40}")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_PATH = re.compile(r"/[A-Za-z0-9_./-]+")


@dataclass(frozen=True)
class Preview:
    pr: int
    head_sha: str
    image_id: str  # sealed image ID, never a mutable tag
    slot: int  # 0..MAX_SLOTS-1 from the admission record; not derived from the PR

    def __post_init__(self) -> None:
        if not isinstance(self.pr, int) or isinstance(self.pr, bool) or not 0 < self.pr < 2**31:
            raise ValueError("pr must be a positive 31-bit integer")
        if not _SHA.fullmatch(self.head_sha):
            raise ValueError("head_sha must be a full lowercase 40-hex SHA")
        if not _IMAGE_ID.fullmatch(self.image_id):
            raise ValueError("image_id must be a sha256:<64 hex> image ID")
        if not isinstance(self.slot, int) or isinstance(self.slot, bool):
            raise ValueError("slot must be an integer")
        if not 0 <= self.slot < MAX_SLOTS:
            raise ValueError(f"slot must be in 0..{MAX_SLOTS - 1}")

    @property
    def name(self) -> str:
        return f"artemis-preview-pr{self.pr}"

    @property
    def bridge(self) -> str:
        return f"{BRIDGE_PREFIX}{self.slot}"

    @property
    def subnet(self) -> IPv4Network:
        return list(POOL.subnets(new_prefix=28))[self.slot]

    @property
    def gateway(self) -> str:
        return str(self.subnet.network_address + 1)

    @property
    def ip(self) -> str:
        return str(self.subnet.network_address + 2)

    @property
    def host_port(self) -> int:
        return HOST_PORT_BASE + self.slot

    @property
    def labels(self) -> dict[str, str]:
        return {
            "artemis.preview": "1",
            "artemis.preview.pr": str(self.pr),
            "artemis.preview.head": self.head_sha,
            "artemis.preview.slot": str(self.slot),
        }


def _label_args(preview: Preview) -> list[str]:
    return [a for k, v in preview.labels.items() for a in ("--label", f"{k}={v}")]


def network_create_argv(preview: Preview) -> list[str]:
    """Per-PR bridge: fixed name for the firewall match, no NAT, no inter-container traffic."""
    return [
        "docker", "network", "create", "--driver", "bridge",
        "--subnet", str(preview.subnet), "--gateway", preview.gateway,
        "-o", f"com.docker.network.bridge.name={preview.bridge}",
        "-o", "com.docker.network.bridge.enable_ip_masquerade=false",
        "-o", "com.docker.network.bridge.enable_icc=false",
        *_label_args(preview),
        preview.name,
    ]  # fmt: skip


def container_create_argv(preview: Preview, env: Mapping[str, str], jwks_bundle: str) -> list[str]:
    """`docker create` argv. `jwks_bundle` is the host's read-only public-key file."""
    extra = set(env) - ALLOWED_ENV
    if extra:
        raise ValueError(f"environment not allowed: {sorted(extra)}")
    if any("\n" in v or "\x00" in v for v in env.values()):
        raise ValueError("environment values must be single-line")
    if not _PATH.fullmatch(jwks_bundle) or ".." in jwks_bundle.split("/"):
        raise ValueError("jwks_bundle must be a plain absolute path")
    merged = {**env, **_FIXED_ENV}  # fixed values win; callers cannot override them
    return [
        "docker", "create", "--name", preview.name, "--network", preview.name,
        "--ip", preview.ip, "--restart", "no", "--stop-timeout", "5",
        # privilege: non-root, no capabilities, no setuid escalation, default seccomp
        "--user", f"{UID}:{UID}", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only", "--ipc", "private", "--init",
        # resources: 1 CPU, 1 GiB with no extra swap, 256 PIDs, 256 MiB tmpfs, 20 MiB logs
        "--cpus", "1", "--memory", "1g", "--memory-swap", "1g", "--pids-limit", str(PIDS_LIMIT),
        "--shm-size", "32m",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--tmpfs", f"{STATE_DIR}:rw,noexec,nosuid,nodev,size=160m,mode=0700,uid={UID},gid={UID}",
        "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
        # network: no IPv6, no real resolver, loopback-only publish for Traefik
        "--sysctl", "net.ipv6.conf.all.disable_ipv6=1",
        "--dns", DNS_BLACKHOLE,
        "--publish", f"127.0.0.1:{preview.host_port}:{CONTAINER_PORT}",
        "--mount", f"type=bind,src={jwks_bundle},dst={JWKS_PATH},readonly",
        *_label_args(preview),
        *[a for k, v in sorted(merged.items()) for a in ("--env", f"{k}={v}")],
        "--entrypoint", ENTRYPOINT,
        preview.image_id, *COMMAND,
    ]  # fmt: skip


def verify_image(image_inspect: Mapping) -> None:
    """Run on `docker image inspect` output before `docker create`.

    Docker mounts an anonymous volume for every image-declared VOLUME even with
    `--read-only`, which would give a PR image writable host-backed storage outside the
    tmpfs budget. There is no run flag to disable it, so such an image is refused.
    """
    volumes = (image_inspect.get("Config") or {}).get("Volumes") or {}
    if volumes:
        raise ValueError(f"image declares volumes: {sorted(volumes)}")


def verify_container(container_inspect: Mapping) -> None:
    """Run on `docker inspect` output of the created container, before `docker start`."""
    mounts = {(m["Type"], m["Destination"], m["RW"]) for m in container_inspect.get("Mounts") or []}
    if mounts != {("bind", JWKS_PATH, False)}:
        raise ValueError(f"unexpected mounts: {sorted(mounts)}")
    host = container_inspect.get("HostConfig") or {}
    if set(host.get("Tmpfs") or {}) != {"/tmp", STATE_DIR}:
        raise ValueError(f"unexpected tmpfs mounts: {sorted(host.get('Tmpfs') or {})}")
    if host.get("Binds") or host.get("VolumesFrom"):
        raise ValueError("unexpected binds or volumes-from")
