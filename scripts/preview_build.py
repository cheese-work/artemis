"""Plan hook-free Node acquisition and verify staged preview build inputs."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import TypedDict
from urllib.parse import urlsplit


@dataclass(frozen=True)
class NodeAcquisitionPlan:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    package_json_sha256: str
    lockfile_sha256: str


class NodeStageManifest(TypedDict):
    package_json_sha256: str
    lockfile_sha256: str
    node_modules_sha256: str
    npm_version: str


def _directory(path: Path) -> Path:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"expected a real directory: {path}")
    return path


def _app(work_dir: Path) -> Path:
    return _directory(_directory(_directory(work_dir) / "apps") / "showcase_ui")


def _regular_file(path: Path) -> Path:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"expected a regular file: {path}")
    return path


def _sha256(path: Path) -> str:
    with _regular_file(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acquire_node_plan(work_dir: Path, proxy_url: str) -> NodeAcquisitionPlan:
    """Read the two metadata files and return a command, without executing it."""
    proxy = urlsplit(proxy_url)
    if proxy.scheme not in {"http", "https"} or not proxy.hostname:
        raise ValueError("acquisition requires an HTTP(S) proxy URL")
    app = _app(work_dir)
    if (app / ".npmrc").exists(follow_symlinks=False):
        raise ValueError("project .npmrc is not allowed during preview acquisition")
    try:
        lock_bytes = _regular_file(app / "package-lock.json").read_bytes()
        lock = json.loads(lock_bytes)
    except (OSError, ValueError) as error:
        raise ValueError("package-lock.json must be readable, regular JSON") from error
    version = lock.get("lockfileVersion") if isinstance(lock, dict) else None
    if type(version) is not int or version < 3:
        raise ValueError("package-lock.json requires an integer lockfileVersion >= 3")
    return NodeAcquisitionPlan(
        argv=("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"),
        cwd=app,
        env={
            "HTTPS_PROXY": proxy_url,
            "npm_config_https_proxy": proxy_url,
            "npm_config_userconfig": "/dev/null",
            "npm_config_globalconfig": "/proc/self/root/dev/null",
        },
        package_json_sha256=_sha256(app / "package.json"),
        lockfile_sha256=hashlib.sha256(lock_bytes).hexdigest(),
    )


def _raise_walk_error(error: OSError) -> None:
    raise error


def _tree_sha256(root: Path) -> str:
    root = _directory(root).resolve(strict=True)
    entries = sorted(
        (directory / name).relative_to(root).as_posix()
        for directory, subdirs, files in root.walk(on_error=_raise_walk_error)
        for name in subdirs + files
    )
    digest = hashlib.sha256()
    for relative in entries:
        path = root / relative
        metadata = path.lstat()
        mode = metadata.st_mode
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            try:
                within_tree = not Path(target).is_absolute() and path.resolve(
                    strict=True
                ).is_relative_to(root)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"invalid node_modules symlink: {relative}") from error
            if not within_tree:
                raise ValueError(f"escaping node_modules symlink: {relative}")
            payload = os.fsencode(target)
            kind, size, content_hash = "symlink", len(payload), hashlib.sha256(payload).hexdigest()
        elif stat.S_ISREG(mode):
            kind, size, content_hash = "file", metadata.st_size, _sha256(path)
        elif stat.S_ISDIR(mode):
            kind, size, content_hash = "directory", 0, None
        else:
            raise ValueError(f"unsupported node_modules entry: {relative}")
        row = (relative, kind, stat.S_IMODE(mode), size, content_hash)
        digest.update(json.dumps(row, separators=(",", ":")).encode("ascii") + b"\n")
    return digest.hexdigest()


def _manifest(app: Path, npm_version: str) -> NodeStageManifest:
    return {
        "package_json_sha256": _sha256(app / "package.json"),
        "lockfile_sha256": _sha256(app / "package-lock.json"),
        "node_modules_sha256": _tree_sha256(app / "node_modules"),
        "npm_version": npm_version,
    }


def stage_manifest(
    work_dir: Path, plan: NodeAcquisitionPlan, npm_version: str
) -> NodeStageManifest:
    """Seal staged inputs against the metadata observed before acquisition."""
    if not npm_version or npm_version != npm_version.strip():
        raise ValueError("expected the npm version used for acquisition")
    manifest = _manifest(_app(work_dir), npm_version)
    if (
        manifest["package_json_sha256"] != plan.package_json_sha256
        or manifest["lockfile_sha256"] != plan.lockfile_sha256
    ):
        raise ValueError("Node input metadata changed after acquisition was planned")
    return manifest


def verify_staged(work_dir: Path, manifest: Mapping[str, object]) -> None:
    """Fail before starting BuildKit if the staged files do not match the trusted record."""
    npm_version = manifest.get("npm_version")
    if not isinstance(npm_version, str) or not npm_version or npm_version != npm_version.strip():
        raise ValueError("invalid staged input manifest: npm version")
    try:
        observed = _manifest(_app(work_dir), npm_version)
    except (OSError, ValueError) as error:
        raise ValueError("staged input differs: unable to verify files") from error
    if observed != manifest:
        raise ValueError("staged input differs from the Node manifest")
