"""Inert sealed-import boundary and namespace-confined archive checker (CHE-1293)."""

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import stat
import subprocess
import sys
import tarfile
import time
from typing import BinaryIO
from uuid import UUID

DOCKER_VERSION = "29.8.2"
DOCKER_IMAGE_INDEX_DIGEST = (
    "sha256:7dcdfc4a20246236f558175182ccace1eb15a41bd3eb119dd2284f393498b7c1"
)
DOCKER_LINUX_AMD64_DIGEST = (
    "sha256:dcac6f16dc25ddec91e2d467605775b95a035ab884b94cb4c2cc7cbef6fd726d"
)
MAX_MANIFEST_BYTES = 16384
MAX_JSON_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_IMAGE_BYTES = 4 * 1024**3
MAX_MEMBERS = 65536
MAX_LAYERS = 128
CHECK_TIMEOUT = 30
_HEX = r"[0-9a-f]{64}"
_LAYER = re.compile(_HEX + r"/layer\.tar")


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    repository: str
    repository_id: int
    pr: int
    head_sha: str
    base_sha: str
    controller_sha: str
    run_id: int
    run_attempt: int
    job_id: int
    runner_id: int
    nonce: str
    admission_id: str
    archive_sha256: str
    archive_size: int
    image_id: str


@dataclass(frozen=True)
class Seal:
    """Protected daemon record, never constructed from a job's comparison values."""

    manifest: Manifest
    manifest_sha256: str


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON number")


def _json(payload: bytes, limit: int):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
        raise ValueError("JSON size limit")
    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
        )
    except (UnicodeError, RecursionError, ValueError) as error:
        raise ValueError("invalid bounded JSON") from error


def _matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def parse_manifest(payload: bytes) -> Manifest:
    values = _json(payload, MAX_MANIFEST_BYTES)
    if not isinstance(values, dict) or set(values) != {field.name for field in fields(Manifest)}:
        raise ValueError("manifest fields do not match schema")
    if type(values["schema_version"]) is not int or values["schema_version"] != 1:
        raise ValueError("unsupported manifest schema")
    if values["repository"] != "cheese-work/artemis":
        raise ValueError("manifest repository not allowed")
    for name in (
        "repository_id",
        "pr",
        "run_id",
        "run_attempt",
        "job_id",
        "runner_id",
        "archive_size",
    ):
        upper = MAX_ARCHIVE_BYTES if name == "archive_size" else 2**63 - 1
        if name == "pr":
            upper = 2**31 - 1
        if type(values[name]) is not int or not 0 < values[name] <= upper:
            raise ValueError("invalid manifest integer")
    for name in ("head_sha", "base_sha", "controller_sha"):
        if not _matches(values[name], r"[0-9a-f]{40}"):
            raise ValueError("manifest requires full lowercase source SHA")
    for name in ("nonce", "archive_sha256"):
        if not _matches(values[name], _HEX):
            raise ValueError("invalid manifest digest/nonce")
    if not _matches(values["image_id"], "sha256:" + _HEX):
        raise ValueError("invalid image ID")
    try:
        if str(UUID(values["admission_id"])) != values["admission_id"]:
            raise ValueError("noncanonical admission ID")
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("invalid admission ID") from error
    return Manifest(**values)


def canonical_manifest(manifest: Manifest) -> bytes:
    return json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":")).encode()


def _deadline(deadline):
    if time.monotonic() > deadline:
        raise ValueError("archive check time limit")


def _chunks(source: BinaryIO, deadline):
    while True:
        _deadline(deadline)
        payload = source.read(128 * 1024)
        if not payload:
            return
        yield payload


class _HashReader:
    def __init__(self, source, deadline):
        self.source, self.deadline = source, deadline
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        _deadline(self.deadline)
        payload = self.source.read(size)
        self.digest.update(payload)
        return payload


def _layer_digest(source: BinaryIO, deadline) -> tuple[str, int]:
    reader = _HashReader(source, deadline)
    expanded = 0
    with tarfile.open(fileobj=reader, mode="r|") as archive:
        for count, member in enumerate(archive, 1):
            _deadline(deadline)
            name = member.name.removeprefix("./")
            if (
                count > MAX_MEMBERS
                or len(member.name) > 4096
                or "\x00" in name
                or "\\" in name
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or member.issparse()
                or not (member.isfile() or member.isdir() or member.issym() or member.islnk())
            ):
                raise ValueError("unsafe layer member")
            if member.issym() or member.islnk():
                target = member.linkname
                parent = (
                    ""
                    if target.startswith("/") or member.islnk()
                    else str(PurePosixPath(name).parent)
                )
                normalized = os.path.normpath((parent + "/" if parent else "") + target.lstrip("/"))
                if (
                    len(target) > 4096
                    or "\x00" in target
                    or "\\" in target
                    or normalized.startswith("../")
                    or normalized == ".."
                ):
                    raise ValueError("unsafe layer link")
            if member.size < 0:
                raise ValueError("negative layer size")
            expanded += member.size
            if expanded > MAX_IMAGE_BYTES:
                raise ValueError("expanded layer size limit")
    for _payload in _chunks(reader, deadline):
        pass
    return "sha256:" + reader.digest.hexdigest(), expanded


def check_archive(source: BinaryIO, expected_image_id: str) -> str:
    """Accept only one tagless, uncompressed legacy Docker-save image; never extract."""

    if not _matches(expected_image_id, "sha256:" + _HEX):
        raise ValueError("invalid expected image ID")
    source.seek(0, os.SEEK_END)
    if not 0 < source.tell() <= MAX_ARCHIVE_BYTES:
        raise ValueError("archive size limit")
    source.seek(0)
    deadline = time.monotonic() + CHECK_TIMEOUT
    names, documents, layers = set(), {}, {}
    expanded = 0
    try:
        with tarfile.open(fileobj=source, mode="r|") as archive:
            for count, member in enumerate(archive, 1):
                _deadline(deadline)
                name = member.name
                if count > MAX_MEMBERS or name in names or member.issparse():
                    raise ValueError("duplicate/sparse archive member or member limit")
                names.add(name)
                if member.isdir() and _matches(name, _HEX):
                    continue
                if not member.isfile() or member.size < 0:
                    raise ValueError("archive must contain regular files only")
                content = archive.extractfile(member)
                if _LAYER.fullmatch(name):
                    if len(layers) >= MAX_LAYERS or member.size > MAX_ARCHIVE_BYTES:
                        raise ValueError("layer count/size limit")
                    layers[name], layer_size = _layer_digest(content, deadline)
                    expanded += layer_size
                    if expanded > MAX_IMAGE_BYTES:
                        raise ValueError("unpacked image size limit")
                elif name == "manifest.json" or _matches(name, _HEX + r"(\.json|/(json|VERSION))"):
                    limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_JSON_BYTES
                    if member.size > limit:
                        raise ValueError("archive JSON size limit")
                    documents[name] = content.read(limit + 1)
                else:
                    raise ValueError("unsafe or unknown archive name")
            tail_offset = archive.offset
        source.seek(tail_offset)
        if any(payload.strip(b"\0") for payload in _chunks(source, deadline)):
            raise ValueError("nonzero archive trailer")
        manifest = _json(documents.get("manifest.json", b""), MAX_MANIFEST_BYTES)
        if (
            not isinstance(manifest, list)
            or len(manifest) != 1
            or not isinstance(manifest[0], dict)
        ):
            raise ValueError("archive must describe exactly one image")
        entry = manifest[0]
        if set(entry) != {"Config", "RepoTags", "Layers"}:
            raise ValueError("unsupported Docker manifest fields")
        if entry["RepoTags"] not in (None, []):
            raise ValueError("archive tags are forbidden")
        config_name = expected_image_id.removeprefix("sha256:") + ".json"
        selected = entry["Layers"]
        if (
            entry["Config"] != config_name
            or not isinstance(selected, list)
            or not 0 < len(selected) <= MAX_LAYERS
            or any(not isinstance(name, str) for name in selected)
            or len(set(selected)) != len(selected)
            or set(selected) != set(layers)
        ):
            raise ValueError("config/layer selection mismatch")
        allowed = {"manifest.json", config_name, *selected}
        for name in selected:
            parent = name.split("/")[0]
            allowed.update({parent, parent + "/json", parent + "/VERSION"})
        if names - allowed:
            raise ValueError("undeclared archive members")
        config_bytes = documents.get(config_name, b"")
        if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != expected_image_id:
            raise ValueError("config image digest mismatch")
        config = _json(config_bytes, MAX_JSON_BYTES)
        if (
            not isinstance(config, dict)
            or config.get("os") != "linux"
            or config.get("architecture") != "amd64"
        ):
            raise ValueError("image platform mismatch")
        runtime = config.get("config", {})
        if not isinstance(runtime, dict) or runtime.get("Volumes"):
            raise ValueError("image-declared volumes are forbidden")
        rootfs = config.get("rootfs")
        if (
            not isinstance(rootfs, dict)
            or rootfs.get("type") != "layers"
            or rootfs.get("diff_ids") != [layers[name] for name in selected]
        ):
            raise ValueError("layer diff ID mismatch")
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError("malformed uncompressed image archive") from error
    return expected_image_id


class SandboxedPrecheck:
    """Run protected checker code with no network, capabilities or writable host mount."""

    def __call__(self, source: BinaryIO, expected_image_id: str) -> str:
        checker = Path(__file__).resolve()
        command = [
            "/usr/bin/bwrap",
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--ro-bind",
            str(checker),
            "/checker.py",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--chdir",
            "/tmp",
            "--",
            "/usr/bin/python3",
            "-I",
            "/checker.py",
            "--check-archive",
            expected_image_id,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=source,
                capture_output=True,
                check=True,
                timeout=CHECK_TIMEOUT,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("sandboxed archive pre-check failed; import stopped") from error
        if result.stdout != (expected_image_id + "\n").encode():
            raise ValueError("sandboxed archive pre-check result mismatch")
        return expected_image_id


class DockerLoad:
    """Bounded fixed-socket transport; construct only inside the protected daemon."""

    def __init__(self, expected_image_id: str):
        if not _matches(expected_image_id, "sha256:" + _HEX):
            raise ValueError("invalid expected image ID")
        self.expected_image_id = expected_image_id

    def _run(self, arguments: list[str], *, source=None, timeout=10):
        return subprocess.run(
            [
                "/usr/bin/docker",
                "--config",
                "/var/empty",
                "--host",
                "unix:///var/run/docker.sock",
                *arguments,
            ],
            stdin=source,
            capture_output=True,
            check=True,
            timeout=timeout,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )

    def __call__(self, source: BinaryIO) -> str:
        version = self._run(["version", "--format", "{{.Server.Version}}"])
        if version.stdout != (DOCKER_VERSION + "\n").encode():
            raise ValueError("engine version changed before import")
        self._run(["image", "load", "--quiet"], source=source, timeout=120)
        inspected = self._run(["image", "inspect", "--format", "{{.Id}}", self.expected_image_id])
        if inspected.stdout != (self.expected_image_id + "\n").encode():
            raise ValueError("loaded image ID mismatch")
        return self.expected_image_id


def _protected(source: BinaryIO, owner_uid: int, size: int):
    metadata = os.fstat(source.fileno())
    if (
        type(owner_uid) is not int
        or owner_uid < 0
        or metadata.st_uid != owner_uid
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o400
        or metadata.st_size != size
        or fcntl.fcntl(source.fileno(), fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY
    ):
        raise ValueError("sealed archive must be protected, read-only and singly linked")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _archive_digest(source):
    source.seek(0)
    result = hashlib.sha256()
    for payload in _chunks(source, time.monotonic() + CHECK_TIMEOUT):
        result.update(payload)
    source.seek(0)
    return result.hexdigest()


def import_sealed(
    source: BinaryIO,
    manifest_bytes: bytes,
    seal: Seal,
    *,
    owner_uid: int,
    engine_version: str,
    engine_digest: str,
    admission: Callable[[Seal], bool],
    load: Callable[[BinaryIO], str],
    precheck: Callable[[BinaryIO, str], str] | None = None,
    enabled: bool = False,
) -> str:
    """Inert trusted-daemon seam. Success returns an image ID, never publishes a route."""

    if enabled is not True:
        raise ValueError("image import is disabled")
    manifest = parse_manifest(manifest_bytes)
    if (
        not isinstance(seal, Seal)
        or manifest != seal.manifest
        or hashlib.sha256(canonical_manifest(manifest)).hexdigest() != seal.manifest_sha256
    ):
        raise ValueError("protected seal does not match manifest")
    if engine_version != DOCKER_VERSION or engine_digest != DOCKER_LINUX_AMD64_DIGEST:
        raise ValueError("unsupported/unverified engine pin")
    original = _protected(source, owner_uid, manifest.archive_size)
    if admission(seal) is not True:
        raise ValueError("admission no longer valid")
    if _archive_digest(source) != manifest.archive_sha256:
        raise ValueError("sealed archive digest mismatch")
    source.seek(0)
    checker = precheck if precheck is not None else SandboxedPrecheck()
    if checker(source, manifest.image_id) != manifest.image_id:
        raise ValueError("pre-check image mismatch")
    if (
        _protected(source, owner_uid, manifest.archive_size) != original
        or _archive_digest(source) != manifest.archive_sha256
    ):
        raise ValueError("sealed archive changed during pre-check")
    if admission(seal) is not True:
        raise ValueError("admission revoked before import")
    source.seek(0)
    if load(source) != manifest.image_id:
        raise ValueError("loaded image ID mismatch")
    return manifest.image_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-archive", required=True)
    arguments = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("archive checker must be unprivileged")
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        print(check_archive(sys.stdin.buffer, arguments.check_archive))
    except (ValueError, MemoryError) as error:
        print(str(error)[:300], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
