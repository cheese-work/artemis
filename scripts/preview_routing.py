"""Inert native-Traefik routing and fixture-safe reconciliation (CHE-1292).

Callers supply protected registry entries and a readiness check. No admission,
image import, Docker access, service installation or live cutover happens here.
"""

from collections.abc import Callable, Sequence
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

from scripts.preview_sandbox import HOST_PORT_BASE, MAX_SLOTS, Preview

HOST = "smart-qa.tevo.vn"
TRAEFIK_VERSION = "3.6.25"
TRAEFIK_LINUX_AMD64_SHA256 = "b3fbb7853887c68b23e6c9c418026bed6ed9eab743e5bd3a76c358472bd55b38"
MAX_CONFIG_BYTES = 65536


def encode(config: dict) -> bytes:
    return (json.dumps(config, sort_keys=True, indent=2) + "\n").encode()


def _port(port: int) -> int:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("port must be an unprivileged integer port")
    return port


def static_config(directory: Path, port: int = 8000) -> dict:
    return {
        "entryPoints": {
            "web": {
                "address": f"127.0.0.1:{_port(port)}",
                "http": {"sanitizePath": False},
                "forwardedHeaders": {"trustedIPs": ["127.0.0.1/32", "::1/128"]},
            }
        },
        "providers": {
            "file": {"directory": str(directory), "watch": True},
            "providersThrottleDuration": "50ms",
        },
        "api": {"dashboard": False, "insecure": False},
        "log": {"level": "ERROR"},
    }


def base_config(live_port: int = 18001, hosts: Sequence[str] = (HOST,)) -> dict:
    if (
        not hosts
        or HOST not in hosts
        or any(
            not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host)
            for host in hosts
        )
    ):
        raise ValueError("supported hosts must be explicit DNS names including SmartQA")
    if _port(live_port) in range(HOST_PORT_BASE, HOST_PORT_BASE + MAX_SLOTS):
        raise ValueError("real-site port overlaps preview registry")
    return {
        "http": {
            "routers": {
                "real-site": {
                    "entryPoints": ["web"],
                    "rule": "("
                    + " || ".join(f"Host(`{host}`)" for host in sorted(set(hosts)))
                    + ") && !(Path(`/preview`) || PathPrefix(`/preview/`))",
                    "priority": 1,
                    "service": "real-site",
                },
                "preview-reserved": {
                    "entryPoints": ["web"],
                    "rule": "Path(`/preview`) || PathPrefix(`/preview/`)",
                    "priority": 100,
                    "service": "api@internal",
                    "middlewares": ["preview-not-found"],
                },
                "ambiguous-path": {
                    "entryPoints": ["web"],
                    "rule": r"PathRegexp(`(^|/)[.]{1,2}(/|$)|//|\\|;|(?i)%(2e|2f|5c|25|00|3b|3f|23)`)",
                    "priority": 1000,
                    "service": "api@internal",
                    "middlewares": ["preview-not-found"],
                },
            },
            "middlewares": {
                "preview-not-found": {"replacePath": {"path": "/__artemis_preview_not_found__"}}
            },
            "services": {
                "real-site": {
                    "loadBalancer": {
                        "passHostHeader": True,
                        "servers": [{"url": f"http://127.0.0.1:{live_port}"}],
                    }
                }
            },
        }
    }


def preview_config(previews: Sequence[Preview]) -> dict:
    if not previews:
        return {}
    if (
        len(previews) > MAX_SLOTS
        or len({item.pr for item in previews}) != len(previews)
        or len({item.slot for item in previews}) != len(previews)
    ):
        raise ValueError("duplicate PR/slot or preview capacity exceeded")
    config = {"http": {"routers": {}, "services": {}, "middlewares": {}}}
    for item in sorted(previews, key=lambda item: item.pr):
        if not isinstance(item, Preview):
            raise ValueError("routes require protected registry Preview entries")
        name = f"preview-pr{item.pr}"
        prefix = f"/preview/pr/{item.pr}"
        config["http"]["routers"][name] = {
            "entryPoints": ["web"],
            "rule": f"Host(`{HOST}`) && (Path(`{prefix}`) || PathPrefix(`{prefix}/`))",
            "priority": 200,
            "service": name,
            "middlewares": [f"{name}-slash", f"{name}-strip"],
        }
        config["http"]["middlewares"][f"{name}-slash"] = {
            "redirectRegex": {
                "regex": rf"^(https?://[^/]+{prefix})(\?.*)?$",
                "replacement": f"https://{HOST}{prefix}/${{2}}",
                "permanent": True,
            }
        }
        config["http"]["middlewares"][f"{name}-strip"] = {"stripPrefix": {"prefixes": [prefix]}}
        config["http"]["services"][name] = {
            "loadBalancer": {
                "passHostHeader": True,
                "servers": [{"url": f"http://127.0.0.1:{item.host_port}"}],
            }
        }
    return config


def validate_preview_config(config: dict) -> None:
    if config == {}:
        return
    try:
        previews = []
        for name in config["http"]["routers"]:
            match = re.fullmatch(r"preview-pr([1-9][0-9]*)", name)
            upstream = config["http"]["services"][name]["loadBalancer"]["servers"][0]["url"]
            if not match or not isinstance(upstream, str):
                raise ValueError("invalid route name or upstream")
            slot = next(
                (
                    slot
                    for slot in range(MAX_SLOTS)
                    if upstream == f"http://127.0.0.1:{HOST_PORT_BASE + slot}"
                ),
                None,
            )
            if slot is None:
                raise ValueError("upstream outside protected slot registry")
            previews.append(Preview(int(match[1]), "0" * 40, "sha256:" + "0" * 64, slot))
        if config != preview_config(previews):
            raise ValueError("preview configuration expands routing policy")
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ValueError("invalid preview configuration") from error


def _read(path: Path) -> bytes | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_CONFIG_BYTES
            or metadata.st_mode & 0o022
        ):
            raise ValueError("configuration must be bounded, regular, protected and singly linked")
        payload = source.read(MAX_CONFIG_BYTES + 1)
        if len(payload) > MAX_CONFIG_BYTES:
            raise ValueError("configuration too large")
        return payload


def _checked(payload: bytes) -> dict:
    config = json.loads(payload)
    validate_preview_config(config)
    return config


class Reconciler:
    def __init__(
        self, base: Path, state: Path, base_sha256: str, validator: Callable[[Path], None]
    ):
        if base.absolute() != base.resolve() or state.absolute() != state.resolve():
            raise ValueError("base and state must be non-symlink paths")
        if base.name in {"previews.yaml", "previews.lkg", ".routes.lock"}:
            raise ValueError("protected base cannot alias reconciler files")
        if not state.is_dir() or state.stat().st_mode & 0o022:
            raise ValueError("state directory must be protected")
        self.base, self.state, self.base_sha256, self.validator = (
            base,
            state,
            base_sha256,
            validator,
        )
        self.active = state / "previews.yaml"
        self.last_good = state / "previews.lkg"

    def _base(self) -> None:
        payload = _read(self.base)
        if payload is None or hashlib.sha256(payload).hexdigest() != self.base_sha256:
            raise ValueError("protected base pin changed or missing")

    @contextmanager
    def _locked(self):
        descriptor = os.open(
            self.state / ".routes.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(descriptor, "rb") as lock:
            metadata = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o022
            ):
                raise ValueError("unsafe lock file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._base()
            for target in (self.active, self.last_good):
                try:
                    metadata = target.lstat()
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_mode & 0o022
                ):
                    raise ValueError("unsafe publication target")
            yield

    def _write(
        self, destination: Path, payload: bytes, before_swap: Callable[[Path], None] | None = None
    ) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".routes-", suffix=".candidate", dir=self.state)
        candidate = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o640)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if before_swap:
                before_swap(candidate)
            self._base()
            os.replace(candidate, destination)
            directory = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            candidate.unlink(missing_ok=True)

    def reconcile(self, previews: Sequence[Preview], *, ready: Callable[[Preview], bool]) -> None:
        payload = encode(preview_config(previews))
        with self._locked():
            if any(ready(item) is not True for item in previews):
                raise ValueError("preview not ready; routes unchanged")
            previous = _read(self.active) or encode(preview_config([]))
            _checked(previous)

            def validate(candidate):
                self.validator(candidate)
                self._write(self.last_good, previous)

            self._write(self.active, payload, validate)
            self._write(self.last_good, payload)

    def recover(self) -> None:
        with self._locked():
            for source in (self.active, self.last_good):
                try:
                    payload = _read(source)
                    if payload is None:
                        continue
                    _checked(payload)
                    self._write(self.active, payload, self.validator)
                except ValueError:
                    continue
                self._write(self.last_good, payload)
                return
            payload = encode(preview_config([]))
            self._write(self.active, payload, self.validator)
            self._write(self.last_good, payload)


class NativeValidator:
    """Validate with the checksum-pinned native binary on disposable loopback ports."""

    def __init__(self, binary: Path, base: Path, base_sha256: str):
        with binary.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != TRAEFIK_LINUX_AMD64_SHA256:
                raise ValueError("native Traefik binary pin mismatch")
        self.binary, self.base, self.base_sha256 = binary.resolve(), base, base_sha256

    def __call__(self, candidate: Path) -> None:
        payload = _read(candidate)
        config = _checked(payload)
        base = _read(self.base)
        if base is None or hashlib.sha256(base).hexdigest() != self.base_sha256:
            raise ValueError("protected base pin changed")
        expected = {
            f"{name}@file"
            for name in json.loads(base)["http"]["routers"]
            | config.get("http", {}).get("routers", {})
        }
        with tempfile.TemporaryDirectory(prefix="traefik-validate-") as name:
            directory = Path(name)
            routes = directory / "routes"
            routes.mkdir()
            (routes / "base.yaml").write_bytes(base)
            (routes / "previews.yaml").write_bytes(payload)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                api_port = listener.getsockname()[1]
            static = static_config(routes)
            static["entryPoints"]["web"]["address"] = "127.0.0.1:0"
            static["entryPoints"]["validation"] = {"address": f"127.0.0.1:{api_port}"}
            (routes / "validation.yaml").write_bytes(
                encode(
                    {
                        "http": {
                            "routers": {
                                "validation": {
                                    "entryPoints": ["validation"],
                                    "rule": "PathPrefix(`/api/`)",
                                    "service": "api@internal",
                                }
                            }
                        }
                    }
                )
            )
            (directory / "static.yaml").write_bytes(encode(static))
            opener = build_opener(ProxyHandler({}))
            with (directory / "native.log").open("wb+") as log:
                process = subprocess.Popen(
                    [str(self.binary), f"--configFile={directory / 'static.yaml'}"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=directory,
                    env={"PATH": "/usr/bin:/bin", "HOME": name, "TMPDIR": name},
                )
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            with opener.open(
                                f"http://127.0.0.1:{api_port}/api/rawdata", timeout=0.5
                            ) as response:
                                runtime = json.load(response)
                            if expected <= runtime.get("routers", {}).keys():
                                if any(
                                    item.get("status") == "disabled" or item.get("error")
                                    for section in runtime.values()
                                    if isinstance(section, dict)
                                    for item in section.values()
                                    if isinstance(item, dict)
                                ):
                                    raise ValueError(
                                        "native configuration has disabled/error objects"
                                    )
                                return
                        except (URLError, TimeoutError, ConnectionError):
                            pass
                        time.sleep(0.05)
                    log.seek(0)
                    raise ValueError(
                        "native configuration did not become valid: "
                        + log.read(4096).decode(errors="replace")
                    )
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def import_preview(archive: Path) -> None:
    raise NotImplementedError("preview image import and live admission are disabled in L4b1")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover protected routes before native ingress starts; no admission"
    )
    parser.add_argument("action", choices=["recover"])
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--base-sha256", required=True)
    parser.add_argument("--traefik", type=Path, required=True)
    args = parser.parse_args()
    validator = NativeValidator(args.traefik, args.base, args.base_sha256)
    Reconciler(args.base, args.state, args.base_sha256, validator).recover()


if __name__ == "__main__":
    main()
