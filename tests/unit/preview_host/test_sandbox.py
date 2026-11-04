"""Hermetic checks for the preview sandbox builders and image contract (CHE-1291)."""

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from scripts import preview_sandbox as sb

ROOT = Path(__file__).resolve().parents[3]
SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
ENV = {"ARTEMIS_AUTH_MODE": "cloudflare", "ARTEMIS_ADMIN_EMAILS": "admin@example.com"}


def preview(slot: int = 0, pr: int = 70) -> sb.Preview:
    return sb.Preview(pr=pr, head_sha=SHA, image_id=IMAGE, slot=slot)


def create_argv(**kw) -> list[str]:
    return sb.container_create_argv(preview(**kw), ENV, "/srv/preview/jwks.json")


def values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--user", "10001:10001"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--ipc", "private"),
        ("--memory", "1g"),
        ("--memory-swap", "1g"),
        ("--cpus", "1"),
        ("--pids-limit", "256"),
        ("--restart", "no"),
    ],
)
def test_container_hardening_flag(flag, value):
    assert value in values(create_argv(), flag)


def test_read_only_rootfs_and_default_seccomp():
    argv = create_argv()
    assert "--read-only" in argv
    assert not [a for a in values(argv, "--security-opt") if "seccomp" in a or "apparmor" in a]


@pytest.mark.parametrize(
    "flag",
    [
        "--privileged",
        "--cap-add",
        "--device",
        "--pid",
        "--userns",
        "--volume",
        "-v",
        "--volumes-from",
    ],
)
def test_forbidden_flags_absent(flag):
    assert flag not in create_argv()


def test_only_mount_is_the_readonly_jwks_bundle():
    argv = create_argv()
    assert values(argv, "--mount") == [
        "type=bind,src=/srv/preview/jwks.json,dst=/run/preview/jwks.json,readonly"
    ]
    assert not any("docker.sock" in a for a in argv)
    assert "host" not in values(argv, "--network")


def test_writable_tmpfs_is_capped_at_256_mib():
    argv = create_argv()
    sizes = [int(re.search(r"size=(\d+)m", t).group(1)) for t in values(argv, "--tmpfs")]
    sizes.append(int(values(argv, "--shm-size")[0].rstrip("m")))
    assert sum(sizes) <= 256
    assert all("noexec" in t and "nosuid" in t and "nodev" in t for t in values(argv, "--tmpfs"))


def test_logs_capped_at_20_mib():
    argv = create_argv()
    opts = dict(o.split("=") for o in values(argv, "--log-opt"))
    assert opts == {"max-size": "10m", "max-file": "2"}


def test_publish_is_loopback_only_on_the_slot_port():
    assert values(create_argv(slot=2), "--publish") == ["127.0.0.1:18102:8080"]


def test_network_denies_ipv6_and_real_dns():
    argv = create_argv()
    assert "net.ipv6.conf.all.disable_ipv6=1" in values(argv, "--sysctl")
    assert values(argv, "--dns") == [sb.DNS_BLACKHOLE]


def test_entrypoint_and_image_are_pinned_by_the_daemon():
    argv = create_argv()
    assert values(argv, "--entrypoint") == ["python"]
    assert argv[argv.index(IMAGE) :] == [IMAGE, *sb.COMMAND]


def test_env_is_fixed_values_plus_allowlisted_inputs_only():
    env = dict(e.split("=", 1) for e in values(create_argv(), "--env"))
    assert env["ARTEMIS_PREVIEW_PROFILE"] == "1"
    assert env["ARTEMIS_APP_DIR"] == sb.STATE_DIR
    assert env["ARTEMIS_PREVIEW_JWKS_BUNDLE"] == sb.JWKS_PATH
    assert env["ARTEMIS_AUTH_MODE"] == "cloudflare"
    assert set(env) - sb.ALLOWED_ENV == set(sb._FIXED_ENV)


@pytest.mark.parametrize(
    "bad_env",
    [
        {"ARTEMIS_PREVIEW_PROFILE": "0"},
        {"ARTEMIS_APP_DIR": "/"},
        {"LD_PRELOAD": "/x"},
        {"ARTEMIS_AUTH_MODE": "a\nLD_PRELOAD=/x"},
    ],
)
def test_untrusted_env_is_refused(bad_env):
    with pytest.raises(ValueError):
        sb.container_create_argv(preview(), bad_env, "/srv/preview/jwks.json")


def test_identity_switch_is_an_explicit_preview_only_input():
    environment = {**ENV, "ARTEMIS_PREVIEW_IDENTITY_SWITCH": "1"}
    argv = sb.container_create_argv(preview(), environment, "/srv/preview/jwks.json")
    values_by_name = dict(value.split("=", 1) for value in values(argv, "--env"))
    assert values_by_name["ARTEMIS_PREVIEW_IDENTITY_SWITCH"] == "1"
    assert values_by_name["ARTEMIS_PREVIEW_PROFILE"] == "1"
    assert "ARTEMIS_PREVIEW_IDENTITY_SWITCH=1" not in values(create_argv(), "--env")


def test_demo_board_is_an_explicit_preview_only_input():
    environment = {**ENV, "ARTEMIS_PREVIEW_DEMO": "1"}
    argv = sb.container_create_argv(preview(), environment, "/srv/preview/jwks.json")
    assert "ARTEMIS_PREVIEW_DEMO=1" in values(argv, "--env")
    assert "ARTEMIS_PREVIEW_DEMO=1" not in values(create_argv(), "--env")


@pytest.mark.parametrize(
    "bundle",
    ["relative.json", "/a,b", "/a/../etc/shadow", "/a b", "/a\nb", "", "/x,readonly=false"],
)
def test_bundle_path_cannot_inject_mount_options(bundle):
    with pytest.raises(ValueError):
        sb.container_create_argv(preview(), ENV, bundle)


@pytest.mark.parametrize(
    "kw",
    [
        {"pr": 0},
        {"pr": -1},
        {"pr": True},
        {"pr": 2**31},
        {"head_sha": "A" * 40},
        {"head_sha": "a" * 39},
        {"head_sha": "main"},
        {"image_id": "artemis:latest"},
        {"image_id": "sha256:" + "b" * 63},
        {"slot": -1},
        {"slot": sb.MAX_SLOTS},
        {"slot": True},
    ],
)
def test_preview_rejects_invalid_identity(kw):
    base = {"pr": 70, "head_sha": SHA, "image_id": IMAGE, "slot": 0}
    with pytest.raises(ValueError):
        sb.Preview(**{**base, **kw})


def test_slots_get_distinct_subnets_inside_the_pool():
    subnets = [preview(slot=s).subnet for s in range(sb.MAX_SLOTS)]
    assert len(set(subnets)) == sb.MAX_SLOTS
    assert all(s.subnet_of(sb.POOL) for s in subnets)
    assert not any(a.overlaps(b) for i, a in enumerate(subnets) for b in subnets[i + 1 :])


def test_same_slot_for_different_prs_shares_the_address_but_not_the_name():
    a, b = preview(pr=1), preview(pr=2)
    assert (a.ip, a.bridge) == (b.ip, b.bridge)
    assert a.name != b.name


def test_network_has_fixed_bridge_no_nat_no_icc():
    argv = sb.network_create_argv(preview(slot=1))
    opts = values(argv, "-o")
    assert "com.docker.network.bridge.name=artemis-pv1" in opts
    assert "com.docker.network.bridge.enable_ip_masquerade=false" in opts
    assert "com.docker.network.bridge.enable_icc=false" in opts
    assert "--internal" not in argv  # an internal network cannot publish the loopback port
    assert values(argv, "--subnet") == [str(preview(slot=1).subnet)]


def test_labels_identify_the_exact_preview_for_scoped_cleanup():
    labels = dict(v.split("=", 1) for v in values(create_argv(), "--label"))
    assert labels["artemis.preview.head"] == SHA
    assert labels["artemis.preview.pr"] == "70"
    assert labels["artemis.preview"] == "1"


def test_firewall_drops_new_connections_from_every_preview_bridge_after_replies():
    rules = sb.FIREWALL_RULESET
    assert rules.startswith("table inet artemis_preview")
    for chain in ("input", "forward"):
        body = rules.split(f"chain {chain} {{")[1].split("}")[0]
        lines = [ln.strip() for ln in body.splitlines() if "iifname" in ln]
        assert lines == [
            'iifname "artemis-pv*" ct state established,related accept',
            'iifname "artemis-pv*" drop',
        ]
        assert "hook " + chain in body and "priority -10" in body


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft not installed")
def test_firewall_ruleset_parses_with_nft():
    result = subprocess.run(
        ["nft", "-c", "-f", "-"], input=sb.FIREWALL_RULESET, text=True, capture_output=True
    )
    if "Operation not permitted" in result.stderr:
        pytest.skip("nft needs CAP_NET_ADMIN here; parsed in the integration fixture instead")
    assert result.returncode == 0, result.stderr


def dockerfile_stages() -> dict[str, str]:
    """Stage name -> body without comments, in file order; unnamed stages are `stage<N>`."""
    stages: dict[str, str] = {}
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if match := re.fullmatch(r"FROM .*?(?: AS (\S+))?", line):
            name = match.group(1) or f"stage{len(stages)}"
            stages[name] = ""
        elif stages and not line.lstrip().startswith("#"):
            stages[name] += line + "\n"
    return stages


def test_preview_image_has_no_adb_and_is_not_root():
    stage = dockerfile_stages()["preview"]
    assert not re.search(r"\b(adb|git|curl|android-tools\S*)\b", stage)
    assert re.search(r"(?m)^USER 10001:10001$", stage)
    assert "ARTEMIS_PREVIEW_PROFILE=1" in stage


def test_preview_image_shares_a_base_that_has_no_adb():
    stages = dockerfile_stages()
    assert re.search(r"(?m)^FROM runtime-base AS preview", (ROOT / "Dockerfile").read_text())
    assert "adb" not in stages["runtime-base"].lower()


def test_default_build_target_is_still_the_live_console_with_adb():
    assert "adb" in list(dockerfile_stages().values())[-1]
    assert sb.UID == 10001


def test_image_declared_volumes_are_refused():
    sb.verify_image({"Config": {"Volumes": None}})
    sb.verify_image({"Config": {}})
    with pytest.raises(ValueError, match="/scratch"):
        sb.verify_image({"Config": {"Volumes": {"/scratch": {}}}})


def container_inspect(**over) -> dict:
    base = {
        "Mounts": [{"Type": "bind", "Destination": sb.JWKS_PATH, "RW": False}],
        "HostConfig": {"Tmpfs": {"/tmp": "rw", sb.STATE_DIR: "rw"}},
    }
    return {**base, **over}


def test_expected_container_mount_set_passes():
    sb.verify_container(container_inspect())


@pytest.mark.parametrize(
    "mounts",
    [
        [],
        [{"Type": "bind", "Destination": sb.JWKS_PATH, "RW": True}],
        [
            {"Type": "bind", "Destination": sb.JWKS_PATH, "RW": False},
            {"Type": "volume", "Destination": "/scratch", "RW": True},
        ],
    ],
)
def test_any_other_mount_set_is_refused(mounts):
    with pytest.raises(ValueError, match="mounts"):
        sb.verify_container(container_inspect(Mounts=mounts))


def test_extra_tmpfs_or_binds_are_refused():
    host = {"Tmpfs": {"/tmp": "rw", sb.STATE_DIR: "rw", "/extra": "rw"}}
    with pytest.raises(ValueError, match="tmpfs"):
        sb.verify_container(container_inspect(HostConfig=host))
    host = {"Tmpfs": {"/tmp": "rw", sb.STATE_DIR: "rw"}, "VolumesFrom": ["other"]}
    with pytest.raises(ValueError, match="volumes-from"):
        sb.verify_container(container_inspect(HostConfig=host))
