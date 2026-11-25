"""Plan hook-free Node and wheel-only Python acquisition; verify staged preview build inputs."""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tomllib
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


BUILD_REQUIREMENTS = Path(__file__).with_name("preview_build_requirements.txt")
PYTHON_INPUTS = "python-inputs"
# pip does not expand a manylinux platform to older tags: list every glibc up to bookworm's
# 2.36, plus the legacy aliases (wheels such as adbutils still ship manylinux1).
PYTHON_PLATFORMS = tuple(
    arg
    for name in (
        *(f"manylinux_2_{n}_x86_64" for n in range(36, 4, -1)),
        "manylinux2014_x86_64",
        "manylinux2010_x86_64",
        "manylinux1_x86_64",
    )
    for arg in ("--platform", name)
)


@dataclass(frozen=True)
class PythonAcquisitionPlan:
    commands: tuple[tuple[str, ...], ...]
    cwd: Path
    env: dict[str, str]
    pyproject_sha256: str
    uv_lock_sha256: str
    build_requirements_sha256: str


class PythonStageManifest(TypedDict):
    pyproject_sha256: str
    uv_lock_sha256: str
    requirements_sha256: str
    build_requirements_sha256: str
    wheelhouse_sha256: str
    pip_version: str


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
                raise ValueError(f"invalid symlink: {relative}") from error
            if not within_tree:
                raise ValueError(f"escaping symlink: {relative}")
            payload = os.fsencode(target)
            kind, size, content_hash = "symlink", len(payload), hashlib.sha256(payload).hexdigest()
        elif stat.S_ISREG(mode):
            kind, size, content_hash = "file", metadata.st_size, _sha256(path)
        elif stat.S_ISDIR(mode):
            kind, size, content_hash = "directory", 0, None
        else:
            raise ValueError(f"unsupported entry: {relative}")
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


def _proxy(proxy_url: str) -> None:
    proxy = urlsplit(proxy_url)
    if proxy.scheme not in {"http", "https"} or not proxy.hostname:
        raise ValueError("acquisition requires an HTTP(S) proxy URL")


PYTHON_PLATFORM_TAGS = frozenset(PYTHON_PLATFORMS[1::2]) | {"any"}


def _wheel_fits_builder(filename: str) -> bool:
    """True if a wheel filename has a tag pip would accept for CPython 3.12 on glibc x86_64."""
    try:
        python, abi, platform = filename.removesuffix(".whl").rsplit("-", 3)[-3:]
    except ValueError:
        return False
    if not set(platform.split(".")) & PYTHON_PLATFORM_TAGS:
        return False
    for tag in python.split("."):
        match = re.fullmatch(r"(py|cp)3(\d*)", tag)
        if not match:
            continue
        minor = int(match[2] or 0)
        if tag == "cp312" and "cp312" in abi.split("."):
            return True
        if minor <= 12 and (
            "none" in abi.split(".") or (match[1] == "cp" and "abi3" in abi.split(".") and minor)
        ):
            return True
    return False


def acquire_python_plan(work_dir: Path, proxy_url: str) -> PythonAcquisitionPlan:
    """Hash the lock metadata and return the wheel-only acquisition commands, without running them.

    The controller must run them on CPython 3.12, Linux x86_64: pip evaluates requirement
    markers on the host, and the wheels target the python:3.12-slim builder (glibc 2.36).
    """
    _proxy(proxy_url)
    work_dir = _directory(work_dir)
    pyproject = _sha256(work_dir / "pyproject.toml")
    lock_bytes = _regular_file(work_dir / "uv.lock").read_bytes()
    try:
        packages = tomllib.loads(lock_bytes.decode())["package"]
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("uv.lock must be readable TOML with packages") from error
    edges = [
        (dep["name"], "marker" in dep)
        for package in packages
        for dep in (
            *package.get("dependencies", []),
            *(d for group in package.get("optional-dependencies", {}).values() for d in group),
            *(d for group in package.get("dev-dependencies", {}).values() for d in group),
        )
    ]
    marker_gated = {
        name
        for name in {name for name, _ in edges}
        if all(gated for target, gated in edges if target == name)
    }
    for package in packages:
        if "registry" not in package.get("source", {}):
            continue
        urls = [str(wheel.get("url", "")) for wheel in package.get("wheels", [])]
        if not urls:
            raise ValueError(
                f"{package['name']}=={package['version']} has no binary wheel; "
                "preview builds do not compile source distributions"
            )
        names = [url.rsplit("/", 1)[-1] for url in urls]
        if not any(_wheel_fits_builder(name) for name in names):
            if package["name"] in marker_gated and not any("linux" in name for name in names):
                continue  # e.g. pywin32: pip skips it on Linux by marker, and the download is binary-only
            raise ValueError(
                f"{package['name']}=={package['version']} has no compatible wheel for "
                "CPython 3.12 on glibc x86_64; preview builds do not compile source distributions"
            )
    out = PYTHON_INPUTS
    download = (
        "pip", "download", "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312",
        "--dest", f"{out}/wheelhouse", *PYTHON_PLATFORMS,
    )  # fmt: skip
    return PythonAcquisitionPlan(
        commands=(
            ("mkdir", "-p", f"{out}/wheelhouse"),
            (
                "uv",
                "export",
                "--frozen",
                "--offline",
                "--no-config",
                "--no-dev",
                "--no-emit-project",
                "--no-emit-workspace",
                "--format",
                "requirements-txt",
                "--output-file",
                f"{out}/requirements.txt",
            ),  # fmt: skip
            ("cp", str(BUILD_REQUIREMENTS), f"{out}/build-requirements.txt"),
            (*download, "-r", f"{out}/requirements.txt"),
            (*download, "-r", f"{out}/build-requirements.txt"),
        ),
        cwd=work_dir,
        env={
            "HTTPS_PROXY": proxy_url,
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        },
        pyproject_sha256=pyproject,
        uv_lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        build_requirements_sha256=_sha256(BUILD_REQUIREMENTS),
    )


def _python_manifest(work_dir: Path, pip_version: str) -> PythonStageManifest:
    inputs = _directory(_directory(work_dir) / PYTHON_INPUTS)
    wheelhouse = _directory(inputs / "wheelhouse")
    if not any(path.name.endswith(".whl") for path in wheelhouse.iterdir()):
        raise ValueError("wheelhouse has no wheels")
    return {
        "pyproject_sha256": _sha256(work_dir / "pyproject.toml"),
        "uv_lock_sha256": _sha256(work_dir / "uv.lock"),
        "requirements_sha256": _sha256(inputs / "requirements.txt"),
        "build_requirements_sha256": _sha256(inputs / "build-requirements.txt"),
        "wheelhouse_sha256": _tree_sha256(wheelhouse),
        "pip_version": pip_version,
    }


def stage_python_manifest(
    work_dir: Path, plan: PythonAcquisitionPlan, pip_version: str
) -> PythonStageManifest:
    """Seal the wheelhouse against the metadata observed before acquisition."""
    if not pip_version or pip_version != pip_version.strip():
        raise ValueError("expected the pip version used for acquisition")
    manifest = _python_manifest(work_dir, pip_version)
    if (
        manifest["pyproject_sha256"] != plan.pyproject_sha256
        or manifest["uv_lock_sha256"] != plan.uv_lock_sha256
        or manifest["build_requirements_sha256"] != plan.build_requirements_sha256
    ):
        raise ValueError("Python input metadata changed after acquisition was planned")
    return manifest


def verify_python_staged(work_dir: Path, manifest: Mapping[str, object]) -> None:
    """Fail before starting BuildKit if the wheelhouse or requirements differ from the record."""
    pip_version = manifest.get("pip_version")
    if not isinstance(pip_version, str) or not pip_version or pip_version != pip_version.strip():
        raise ValueError("invalid staged input manifest: pip version")
    try:
        observed = _python_manifest(work_dir, pip_version)
    except (OSError, ValueError) as error:
        raise ValueError("staged input differs: unable to verify files") from error
    if observed != manifest:
        raise ValueError("staged input differs from the Python manifest")
