"""Hostile handoff checks; no Docker daemon, device or live route access."""

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from scripts import preview_import as boundary
from scripts import preview_routing as routing


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def image_archive(extra=(), *, tags=None, config_override=None, layer_override=None):
    layer = b"\0" * 10240 if layer_override is None else layer_override
    config = json.dumps(
        config_override
        or {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest(layer)]},
        }
    ).encode()
    image_id = "sha256:" + digest(config)
    layer_name = "b" * 64 + "/layer.tar"
    contents = [
        (
            "manifest.json",
            json.dumps(
                [{"Config": digest(config) + ".json", "RepoTags": tags, "Layers": [layer_name]}]
            ).encode(),
        ),
        (digest(config) + ".json", config),
        (layer_name, layer),
    ]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in contents:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        for member in extra:
            archive.addfile(member, io.BytesIO(b"x" * member.size))
    return output.getvalue(), image_id


@pytest.fixture
def handoff(tmp_path):
    payload, image_id = image_archive()
    manifest = {
        "schema_version": 1,
        "repository": "cheese-work/artemis",
        "repository_id": 123,
        "pr": 70,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "controller_sha": "c" * 40,
        "run_id": 100,
        "run_attempt": 1,
        "job_id": 200,
        "runner_id": 300,
        "nonce": "d" * 64,
        "admission_id": "01a115eb-54c6-784c-956a-31c1f331a52c",
        "archive_sha256": digest(payload),
        "archive_size": len(payload),
        "image_id": image_id,
    }
    manifest_bytes = json.dumps(manifest).encode()
    parsed = boundary.parse_manifest(manifest_bytes)
    seal = boundary.Seal(parsed, digest(boundary.canonical_manifest(parsed)))
    path = tmp_path / "sealed.tar"
    path.write_bytes(payload)
    path.chmod(0o400)
    return path, manifest, manifest_bytes, seal


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("pr", True),
        ("pr", 0),
        ("pr", 2**31),
        ("run_id", "100"),
        ("run_attempt", -1),
        ("job_id", 1.0),
        ("repository", "other/artemis"),
        ("head_sha", "a" * 7),
        ("base_sha", "A" * 40),
        ("controller_sha", "../controller"),
        ("nonce", "nonce"),
        ("archive_sha256", "../file"),
        ("archive_size", boundary.MAX_ARCHIVE_BYTES + 1),
        ("image_id", "latest"),
        ("admission_id", "not-a-uuid"),
        ("archive_path", "/etc/passwd"),
        ("runtime_options", {"privileged": True}),
    ],
)
def test_hostile_manifest_rejected(handoff, field, value):
    _, manifest, _, _ = handoff
    manifest[field] = value
    with pytest.raises(ValueError):
        boundary.parse_manifest(json.dumps(manifest).encode())


@pytest.mark.parametrize(
    "payload", [b"[]", b"null", b"\xff", b"{}", b'{"pr":1,"pr":2}', b"x" * 17000]
)
def test_invalid_duplicate_or_oversized_manifest_rejected(payload):
    with pytest.raises(ValueError):
        boundary.parse_manifest(payload)


def test_canonical_manifest_ignores_only_json_formatting(handoff):
    _, manifest, _, seal = handoff
    parsed = boundary.parse_manifest(json.dumps(manifest, indent=4, sort_keys=True).encode())
    assert digest(boundary.canonical_manifest(parsed)) == seal.manifest_sha256


@pytest.mark.parametrize(
    "name",
    [
        "/etc/passwd",
        "../escape",
        "a/../../escape",
        "./manifest.json",
        "a\\b",
        "unknown",
        "manifest.json",
    ],
)
def test_unsafe_unknown_or_duplicate_outer_names_rejected(name):
    member = tarfile.TarInfo(name)
    payload, image_id = image_archive([member])
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload), image_id)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]
)
def test_outer_links_and_special_files_rejected(kind):
    member = tarfile.TarInfo("b" * 64 + "/json")
    member.type = kind
    member.linkname = "/etc/passwd"
    payload, image_id = image_archive([member])
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload), image_id)


@pytest.mark.parametrize("tags", [["real-site:latest"], ["artemis-preview:latest"]])
def test_archive_cannot_overwrite_mutable_image_tags(tags):
    payload, image_id = image_archive(tags=tags)
    with pytest.raises(ValueError, match="tag"):
        boundary.check_archive(io.BytesIO(payload), image_id)


def test_valid_tagless_image_checks_config_and_diff_ids(handoff):
    path, _, _, seal = handoff
    with path.open("rb") as source:
        assert boundary.check_archive(source, seal.manifest.image_id) == seal.manifest.image_id


def test_wrong_config_image_id_rejected():
    payload, _ = image_archive()
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload), "sha256:" + "f" * 64)


def test_wrong_layer_diff_id_rejected():
    config = {
        "os": "linux",
        "architecture": "amd64",
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "f" * 64]},
    }
    payload, image_id = image_archive(config_override=config)
    with pytest.raises(ValueError, match="layer"):
        boundary.check_archive(io.BytesIO(payload), image_id)


def test_image_cannot_create_implicit_writable_host_volumes():
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"Volumes": {"/unbounded-state": {}}},
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest(b"\0" * 10240)]},
    }
    payload, image_id = image_archive(config_override=config)
    with pytest.raises(ValueError, match="volumes"):
        boundary.check_archive(io.BytesIO(payload), image_id)


@pytest.mark.parametrize("layer", [b"not a tar", b"\x1f\x8bcompressed"])
def test_malformed_or_compressed_layers_rejected(layer):
    payload, image_id = image_archive(layer_override=layer)
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload), image_id)


def test_nonzero_trailing_archive_data_rejected():
    payload, image_id = image_archive()
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload + b"hidden archive"), image_id)


def invoke(handoff, *, load=None, admission=None, precheck=None, **overrides):
    path, _, manifest, seal = handoff
    options = {
        "owner_uid": os.getuid(),
        "engine_version": boundary.DOCKER_VERSION,
        "engine_digest": boundary.DOCKER_LINUX_AMD64_DIGEST,
        "admission": admission or (lambda receipt: True),
        "precheck": precheck or (lambda source, expected: boundary.check_archive(source, expected)),
        "load": load or (lambda source: seal.manifest.image_id),
        "enabled": True,
    }
    options.update(overrides)
    with path.open("rb") as source:
        return boundary.import_sealed(source, manifest, seal, **options)


def test_import_is_inert_by_default(handoff):
    with pytest.raises(ValueError, match="disabled"):
        invoke(handoff, enabled=False)


def test_import_uses_held_descriptor_and_revalidates_admission(handoff):
    path, _, _, seal = handoff
    payload = path.read_bytes()
    events = []

    def admission(receipt):
        assert receipt == seal
        events.append("admission")
        return True

    def load(source):
        events.append("load")
        assert source.tell() == 0
        assert source.read() == payload
        return seal.manifest.image_id

    assert invoke(handoff, load=load, admission=admission) == seal.manifest.image_id
    assert events == ["admission", "admission", "load"]


@pytest.mark.parametrize("version", ["28.0.0", "29.8.1", "29.9.0", "29.8.2-custom"])
def test_only_pinned_engine_version_is_accepted(handoff, version):
    with pytest.raises(ValueError, match="engine"):
        invoke(handoff, engine_version=version)


def test_engine_artifact_digest_must_match_owner_receipt(handoff):
    with pytest.raises(ValueError, match="engine"):
        invoke(handoff, engine_digest="sha256:" + "f" * 64)


def test_wrong_archive_digest_never_calls_import(handoff):
    path, manifest, manifest_bytes, seal = handoff
    path.chmod(0o600)
    path.write_bytes(b"x" * len(path.read_bytes()))
    path.chmod(0o400)
    with pytest.raises(ValueError, match="digest"):
        invoke((path, manifest, manifest_bytes, seal), load=lambda source: pytest.fail("imported"))


@pytest.mark.parametrize(
    "field,value",
    [("head_sha", "e" * 40), ("run_attempt", 2), ("job_id", 201), ("controller_sha", "f" * 40)],
)
def test_manifest_substitution_never_calls_import(handoff, field, value):
    path, manifest, _, seal = handoff
    manifest[field] = value
    with pytest.raises(ValueError, match="seal"):
        invoke(
            (path, manifest, json.dumps(manifest).encode(), seal),
            load=lambda source: pytest.fail("imported"),
        )


def test_changed_canonical_manifest_hash_rejected(handoff):
    path, manifest, manifest_bytes, seal = handoff
    with pytest.raises(ValueError, match="seal"):
        invoke((path, manifest, manifest_bytes, replace(seal, manifest_sha256="f" * 64)))


@pytest.mark.parametrize("mode", [0o600, 0o440, 0o404])
def test_non_private_or_writable_sealed_file_rejected(handoff, mode):
    path, _, _, _ = handoff
    path.chmod(mode)
    with pytest.raises(ValueError, match="protected"):
        invoke(handoff)


def test_hardlinked_sealed_file_rejected(handoff):
    path, _, _, _ = handoff
    os.link(path, path.with_suffix(".alias"))
    with pytest.raises(ValueError, match="protected"):
        invoke(handoff)


def test_admission_revocation_before_load_rejected(handoff):
    approvals = iter([True, False])
    with pytest.raises(ValueError, match="admission"):
        invoke(
            handoff,
            admission=lambda seal: next(approvals),
            load=lambda source: pytest.fail("imported"),
        )


def test_failed_import_leaves_active_backup_and_base_routes_intact(handoff, tmp_path):
    state = tmp_path / "routes"
    state.mkdir(mode=0o700)
    base = state / "base.yaml"
    base.write_bytes(routing.encode(routing.base_config()))
    base.chmod(0o444)
    reconciler = routing.Reconciler(base, state, digest(base.read_bytes()), lambda candidate: None)
    reconciler.reconcile([], ready=lambda preview: True)
    previous = [target.read_bytes() for target in (base, reconciler.active, reconciler.last_good)]

    def fail(source):
        raise RuntimeError("fixture Docker import failed")

    with pytest.raises(RuntimeError, match="import failed"):
        invoke(handoff, load=fail)
    assert previous == [
        target.read_bytes() for target in (base, reconciler.active, reconciler.last_good)
    ]


def test_precheck_failure_and_wrong_loaded_image_never_publish(handoff):
    def reject(source, expected):
        raise ValueError("hostile archive")

    with pytest.raises(ValueError, match="hostile"):
        invoke(handoff, precheck=reject, load=lambda source: pytest.fail("imported"))
    with pytest.raises(ValueError, match="loaded image"):
        invoke(handoff, load=lambda source: "sha256:" + "f" * 64)


@pytest.mark.parametrize(
    "name,kind,target",
    [
        ("../../escape", tarfile.REGTYPE, ""),
        ("/etc/passwd", tarfile.REGTYPE, ""),
        ("etc/link", tarfile.SYMTYPE, "../../escape"),
        ("link", tarfile.LNKTYPE, "../escape"),
        ("device", tarfile.CHRTYPE, ""),
    ],
)
def test_hostile_layer_members_rejected(name, kind, target):
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, target
        archive.addfile(member)
    payload, image_id = image_archive(layer_override=layer.getvalue())
    with pytest.raises(ValueError):
        boundary.check_archive(io.BytesIO(payload), image_id)


def test_precheck_has_private_namespaces_no_caps_no_credentials_and_timeout(handoff, monkeypatch):
    path, _, _, seal = handoff

    def run(command, **options):
        assert command[0] == "/usr/bin/bwrap"
        for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
            assert flag in command
        assert command[command.index("--uid") + 1] == "65534"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert options["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        assert options["timeout"] == boundary.CHECK_TIMEOUT
        assert "--bind" not in command and "--share-net" not in command
        assert options["stdin"].read(1)
        return subprocess.CompletedProcess(
            command, 0, (seal.manifest.image_id + "\n").encode(), b""
        )

    monkeypatch.setattr(subprocess, "run", run)
    with path.open("rb") as source:
        assert (
            boundary.SandboxedPrecheck()(source, seal.manifest.image_id) == seal.manifest.image_id
        )


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("bwrap", 30), subprocess.CalledProcessError(1, "bwrap")]
)
def test_sandbox_timeout_or_missing_namespace_never_falls_back(handoff, monkeypatch, failure):
    def fail(*arguments, **options):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="import stopped"):
        invoke(
            handoff,
            precheck=boundary.SandboxedPrecheck(),
            load=lambda source: pytest.fail("imported"),
        )


def test_actual_unprivileged_worker_on_disposable_archive(handoff):
    path, _, _, seal = handoff
    with path.open("rb") as source:
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                str(Path(boundary.__file__).resolve()),
                "--check-archive",
                seal.manifest.image_id,
            ],
            stdin=source,
            capture_output=True,
            timeout=boundary.CHECK_TIMEOUT,
            check=True,
            env={"PATH": "/usr/bin:/bin"},
        )
    assert result.stdout == (seal.manifest.image_id + "\n").encode()


def test_native_docker_transport_has_fixed_socket_version_pin_and_load_timeout(
    handoff, monkeypatch
):
    _, _, _, seal = handoff
    commands = []

    def run(command, **options):
        commands.append(command)
        assert command[:5] == [
            "/usr/bin/docker",
            "--config",
            "/var/empty",
            "--host",
            "unix:///var/run/docker.sock",
        ]
        assert options["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        if "version" in command:
            payload = boundary.DOCKER_VERSION
        elif "load" in command:
            assert options["timeout"] == 120
            assert options["stdin"].tell() == 0
            payload = ""
        else:
            assert command[-1] == seal.manifest.image_id
            payload = seal.manifest.image_id
        return subprocess.CompletedProcess(command, 0, (payload + "\n").encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert (
        invoke(handoff, load=boundary.DockerLoad(seal.manifest.image_id)) == seal.manifest.image_id
    )
    assert len(commands) == 3


def test_archive_mutation_during_precheck_is_rejected(handoff):
    path, _, _, seal = handoff

    def mutate(source, expected):
        path.chmod(0o600)
        path.write_bytes(b"x" * seal.manifest.archive_size)
        path.chmod(0o400)
        return expected

    with pytest.raises(ValueError, match="changed"):
        invoke(handoff, precheck=mutate, load=lambda source: pytest.fail("imported"))
