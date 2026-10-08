# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lifecycle of the Artemis Accessibility Helper APK on each Android device.

The helper (``packages/artemis-accessibility-helper``) is an accessibility
service that serves the UI hierarchy over a loopback HTTP port on the phone.
This module owns everything *around* that HTTP API, split by lifetime:

* **Provision** (persistent on the device, survives reboots): the APK is
  installed at the bundled version and the service is enabled in secure
  settings. Only callers that hold the device (a task inside its
  ``DeviceExecutionLock`` boundary, ``artemis helper install``,
  ``mobile_diagnose(attempt_fix=True)``) provision; read-only observers never
  install anything. A cross-process file mutex serialises concurrent installs.
* **Attach** (one process, one device, one session): a health check
  (``/ping``) over an adb-server ``tcp:PORT`` stream to the fixed device port
  (:mod:`artemis.runtime.helper_stream`). There is no ``adb forward`` and no
  host port: a stream follows whichever adb server the manager's endpoint names,
  so the helper is reachable the same way through a local server or a host
  agent's tunnel. A session is identified by endpoint plus serial, so the same
  serial behind two adb servers never shares state.
* **Serve** (per request): handled by :class:`AccessibilityClient`, one stream
  per request, closed before it returns; on a transport error it calls
  :meth:`AccessibilityHelperManager.reattach` once.
* **Detach / evict**: nothing is held open between requests, so detaching only
  forgets the session. A physical unplug needs no host work at all and the
  on-device service keeps running. The ``transport_id`` that ``adb devices -l``
  reports changes on every replug, so a session records it and :meth:`attach`
  treats a changed id as "stale, re-check the helper".
* **Token**: the loopback port is reachable by every app on the phone, so the
  helper serves nothing (except ``/ping``) without the session token this host
  pushed. The token is one random secret per host, kept in a file under the
  Artemis temp directory so every Artemis process on the machine presents the
  same one, and delivered with an ``am broadcast`` that only the adb shell user
  can send (the receiver is guarded by ``WRITE_SECURE_SETTINGS``). It is pushed
  on every attach and again whenever the helper answers 401 (a re-bound service
  starts without one).

Locking is per device: provisioning one phone (up to a minute when the install
is slow) never blocks attaching to another.

**UiAutomation suppresses the helper.** Android unbinds every accessibility
service while a ``UiAutomation`` connection created without
``FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES`` is alive. uiautomator2's device
server (``app_process ... com.wetest.uia2.Main``) and Appium's UiAutomator2
server both do that by default, so the helper is silent for as long as one of
them runs. When the helper does not answer, :meth:`attach` looks for such a
holder: uiautomator2's server (Artemis's own fallback, or one left behind by a
previous run) is stopped so the system re-binds the helper within a second;
Appium's is never touched, the error names it instead.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from typing import Any
import urllib.error

from artemis.config.constants import ENV_ARTEMIS_HELPER_AUTO_INSTALL
from artemis.config.paths import get_temp_dir
from artemis.runtime import helper_stream
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.endpoint_transport import EndpointTransport, EndpointUnreachable
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

PACKAGE_NAME = "com.artemis.helper"
SERVICE_NAME = f"{PACKAGE_NAME}/.ArtemisAccessibilityService"
TOKEN_RECEIVER = f"{PACKAGE_NAME}/.TokenReceiver"
TOKEN_ACTION = f"{PACKAGE_NAME}.SET_TOKEN"
#: Loopback port the service binds on the device. Never used as a host port.
DEVICE_PORT = 18888
#: Oldest helper HTTP contract this host can talk to (see PROTOCOL_VERSION in the APK).
MIN_PROTOCOL_VERSION = 2
TOKEN_FILE_NAME = "session.token"

HELPER_PACKAGE_DIR = (
    Path(__file__).resolve().parent.parent.parent / "packages" / "artemis-accessibility-helper"
)
BUNDLED_APK_PATH = HELPER_PACKAGE_DIR / "ArtemisAccessibilityHelper.apk"
BUNDLED_MANIFEST_PATH = HELPER_PACKAGE_DIR / "helper_manifest.json"

_PING_ATTEMPTS_AFTER_PROVISION = 12
_PING_ATTEMPTS_LAZY = 3
_PING_INTERVAL_SECONDS = 0.5
_UIAUTOMATION_RELEASE_SETTLE_SECONDS = 1.0

#: Process command-line markers of UiAutomation holders. The first group is
#: uiautomator2 (openatx), which Artemis itself uses as the fallback backend and
#: may stop; the second is Appium's server, which belongs to someone else.
_UIAUTOMATOR2_MARKERS = ("com.wetest.uia2.Main", "com.github.uiautomator")
_APPIUM_MARKERS = ("io.appium.uiautomator2",)
_PROVISION_MUTEX_TIMEOUT_SECONDS = 90.0
_PROVISION_MUTEX_STALE_SECONDS = 180.0
_ENABLE_ATTEMPTS = 5
_ENABLE_SETTLE_SECONDS = 0.4
_REVIVE_SETTLE_SECONDS = 0.3

AdbRunner = Callable[[list[str]], subprocess.CompletedProcess]
#: Health check for one device: ``serial -> /ping answer`` (``None`` when silent).
Pinger = Callable[[str], dict[str, Any] | None]
#: Progress callback: ``(event, details)`` with events ``installing`` / ``upgrading`` /
#: ``enabling`` fired *before* the slow step so a UI can show what is happening.
ProvisionEvent = Callable[[str, dict[str, Any]], None]

#: Where a person enables the service by hand when secure settings are rejected
#: (some OEM ROMs and Android 13+ restricted settings for sideloaded apps).
MANUAL_ENABLE_PATH = (
    "Settings > Accessibility > Installed apps (or Downloaded apps) > "
    "Artemis Accessibility Helper > turn it on"
)
ACCESSIBILITY_SETTINGS_INTENT = "android.settings.ACCESSIBILITY_SETTINGS"


class HelperUnavailable(RuntimeError):
    """The helper could not be reached on a device (not installed, disabled, or dead)."""


def auto_install_allowed() -> bool:
    """Whether a task may install / upgrade the helper on a device it holds.

    ``ARTEMIS_HELPER_AUTO_INSTALL`` (environment, then settings); default true.
    """
    raw = os.environ.get(ENV_ARTEMIS_HELPER_AUTO_INSTALL)
    if raw is None:
        try:
            from artemis.config import settings

            value = getattr(settings, "ARTEMIS_HELPER_AUTO_INSTALL", True)
            return (
                bool(value)
                if not isinstance(value, str)
                else value.strip().lower()
                not in (
                    "0",
                    "false",
                    "no",
                    "off",
                )
            )
        except (ImportError, ValueError):
            return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class BundledHelper:
    """The APK shipped in the repository, described by ``helper_manifest.json``."""

    apk_path: Path
    version_code: int
    version_name: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "apk_path": str(self.apk_path),
            "version_code": self.version_code,
            "version_name": self.version_name,
            "sha256": self.sha256,
        }


def load_bundled_helper(
    apk_path: Path = BUNDLED_APK_PATH, manifest_path: Path = BUNDLED_MANIFEST_PATH
) -> BundledHelper | None:
    """Read the bundled APK's manifest; ``None`` when the artifact is not shipped."""
    if not apk_path.is_file() or not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return BundledHelper(
            apk_path=apk_path,
            version_code=int(data["version_code"]),
            version_name=str(data.get("version_name", "")),
            sha256=str(data.get("sha256", "")),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning(f"Bundled accessibility helper manifest is unreadable: {exc}")
        return None


@dataclass
class HelperSession:
    """One process's live tunnel to the helper on one device."""

    serial: str
    transport_id: str | None
    version_code: int | None
    version_name: str
    #: Identity of the adb endpoint the session lives on (``AdbEndpoint.identity``).
    endpoint: str = ""
    attached_at: float = field(default_factory=time.time)
    #: Session token the helper expects in ``X-Artemis-Token``; None for pre-token helpers.
    token: str | None = field(default=None, repr=False)
    protocol_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("token", None)
        return data


@dataclass
class ProvisionResult:
    ok: bool
    action: str  # "up_to_date" | "installed" | "upgraded" | "skipped" | "failed"
    installed_version: int | None
    bundled_version: int | None
    enabled: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AccessibilityHelperManager:
    """Registry of helper sessions for one adb endpoint, keyed by device serial.

    Bound to ``transport`` when given; otherwise to the process's adb endpoint,
    resolved on every call so a worker (whose environment names its run's
    endpoint) and a long-lived server both reach the right adb server.
    """

    def __init__(
        self,
        run_adb: AdbRunner | None = None,
        ping: Pinger | None = None,
        bundled: BundledHelper | None | Callable[[], BundledHelper | None] = load_bundled_helper,
        sleep: Callable[[float], None] = time.sleep,
        token_path: Path | None = None,
        auto_install: Callable[[], bool] = auto_install_allowed,
        transport: EndpointTransport | None = None,
    ) -> None:
        self._transport_override = transport
        self._run_adb = run_adb or self._default_run_adb
        self._ping = ping or self._default_ping
        self._bundled_source = bundled
        self._sleep = sleep
        self._token_path = token_path
        self._auto_install = auto_install
        self._token: str | None = None
        self._sessions: dict[tuple[str, str], HelperSession] = {}
        #: Guards the session registry and the per-device lock table only.
        self._registry_lock = threading.Lock()
        self._device_locks: dict[tuple[str, str], threading.RLock] = {}

    @property
    def transport(self) -> EndpointTransport:
        return self._transport_override or EndpointTransport.shared(None)

    def _key(self, serial: str) -> tuple[str, str]:
        return (self.transport.endpoint.identity, serial)

    def _device_lock(self, serial: str) -> threading.RLock:
        """One re-entrant lock per device, so devices provision and attach in parallel."""
        key = self._key(serial)
        with self._registry_lock:
            lock = self._device_locks.get(key)
            if lock is None:
                lock = self._device_locks[key] = threading.RLock()
            return lock

    def _default_run_adb(
        self, args: list[str], timeout: float = 30.0
    ) -> subprocess.CompletedProcess:
        try:
            return self.transport.run(
                args,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except EndpointUnreachable as exc:
            return subprocess.CompletedProcess(args, 255, stdout="", stderr=str(exc))

    def _default_ping(self, serial: str, timeout: float = 2.0) -> dict[str, Any] | None:
        try:
            raw = self.http(serial, "/ping", timeout=timeout)
            data = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        if not isinstance(data, dict) or not data.get("success"):
            return None
        return data

    def http(
        self,
        serial: str,
        path: str,
        payload: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 6.0,
    ) -> bytes:
        """One request to the helper on ``serial`` over an adb stream (see ``helper_stream``)."""
        return helper_stream.request(
            self.transport,
            serial,
            DEVICE_PORT,
            "GET" if payload is None else "POST",
            path,
            body=payload,
            headers=headers,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ #
    # Bundled artifact
    # ------------------------------------------------------------------ #

    @property
    def bundled(self) -> BundledHelper | None:
        source = self._bundled_source
        return source() if callable(source) else source

    # ------------------------------------------------------------------ #
    # Session token
    # ------------------------------------------------------------------ #

    def host_token(self) -> str:
        """The per-host secret every Artemis process presents to the helper.

        Created on first use (48 hex chars) in the Artemis temp directory with
        owner-only permissions; shared by the MCP daemon, the CLI and the SDK.
        """
        if self._token:
            return self._token
        path = self._token_path or (get_temp_dir("helper-token") / TOKEN_FILE_NAME)
        with self._file_mutex(f"host-token:{path.resolve()}", "the host session token"):
            return self._load_or_create_token(path)

    def _load_or_create_token(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{32,128}", text):
                self._token = text
                return text
        except OSError:
            pass
        token = secrets.token_hex(24)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError as exc:
            # No persistent file (read-only temp?): a process-local token still
            # protects the helper; other processes will simply push their own.
            logger.warning(f"Could not persist the helper session token at {path}: {exc}")
        self._token = token
        return token

    def push_token(self, serial: str) -> bool:
        """Deliver the host token to the helper on ``serial`` (idempotent).

        The broadcast is explicit (component named) so it reaches the manifest
        receiver on every Android version, and it is sent by the adb shell user,
        the only sender the receiver's permission guard admits.
        """
        result = self._adb(
            serial,
            "shell",
            "am",
            "broadcast",
            "-n",
            TOKEN_RECEIVER,
            "-a",
            TOKEN_ACTION,
            "--es",
            "token",
            self.host_token(),
        )
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        ok = result.returncode == 0 and "Broadcast completed" in output
        if not ok:
            logger.warning(
                f"Pushing the session token to the accessibility helper on {serial} failed: "
                f"adb exited with code {result.returncode}."
            )
        return ok

    # ------------------------------------------------------------------ #
    # Device queries (no side effects)
    # ------------------------------------------------------------------ #

    def _adb(self, serial: str, *args: str) -> subprocess.CompletedProcess:
        return self._run_adb(["-s", serial, *args])

    def transport_id(self, serial: str) -> str | None:
        """The adb transport id of ``serial``; changes every time the device reconnects."""
        try:
            result = self._run_adb(["devices", "-l"])
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"adb devices -l failed while reading transport id: {exc}")
            return None
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != serial:
                continue
            for token in parts[2:]:
                if token.startswith("transport_id:"):
                    return token.split(":", 1)[1]
            return None
        return None

    def installed_version(self, serial: str) -> int | None:
        """versionCode of the helper on the device, or ``None`` when it is not installed."""
        result = self._adb(serial, "shell", "dumpsys", "package", PACKAGE_NAME)
        output = result.stdout or ""
        if result.returncode != 0 or "Unable to find package" in output:
            return None
        match = re.search(r"versionCode=(\d+)", output)
        if match is None:
            # dumpsys answered but without a version block: the package is absent.
            return None
        return int(match.group(1))

    def is_service_enabled(self, serial: str) -> bool:
        result = self._adb(
            serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"
        )
        enabled = (result.stdout or "").strip()
        return SERVICE_NAME in enabled.split(":") if enabled and enabled != "null" else False

    def ping(self, serial: str) -> dict[str, Any] | None:
        return self._ping(serial)

    def uiautomation_holders(self, serial: str) -> dict[str, list[int]]:
        """Processes on ``serial`` holding a UiAutomation connection that mutes the helper.

        Returns ``{"uiautomator2": [pids], "appium": [pids]}``.
        """
        holders: dict[str, list[int]] = {"uiautomator2": [], "appium": []}
        try:
            result = self._adb(serial, "shell", "ps", "-A", "-o", "PID,ARGS")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"ps on {serial} failed while looking for UiAutomation holders: {exc}")
            return holders
        for line in (result.stdout or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid, args = int(parts[0]), parts[1]
            if args.lstrip().startswith("sh "):
                continue  # the `sh -c` wrapper around app_process, not the holder itself
            if any(marker in args for marker in _UIAUTOMATOR2_MARKERS):
                holders["uiautomator2"].append(pid)
            elif any(marker in args for marker in _APPIUM_MARKERS):
                holders["appium"].append(pid)
        return holders

    def stop_uiautomator2_server(self, serial: str, pids: list[int] | None = None) -> bool:
        """Kill uiautomator2's device server so Android re-binds the helper.

        Only ever aimed at openatx's server (``u2.jar`` / ``com.github.uiautomator``),
        which Artemis owns as its fallback backend. Returns whether anything was stopped.
        """
        if pids is None:
            pids = self.uiautomation_holders(serial)["uiautomator2"]
        stopped = False
        for pid in pids:
            result = self._adb(serial, "shell", "kill", str(pid))
            stopped = stopped or result.returncode == 0
        result = self._adb(serial, "shell", "am", "force-stop", "com.github.uiautomator")
        stopped = stopped or (result.returncode == 0 and bool(pids))
        if stopped:
            logger.info(
                f"Stopped uiautomator2's device server on {serial} so the accessibility helper "
                "can bind again (UiAutomation suppresses other accessibility services)."
            )
            self._sleep(_UIAUTOMATION_RELEASE_SETTLE_SECONDS)
        return stopped

    def session(self, serial: str) -> HelperSession | None:
        with self._registry_lock:
            return self._sessions.get(self._key(serial))

    def status(self, serial: str) -> dict[str, Any]:
        """Everything a diagnostic wants to know, without changing the device.

        ``reachable`` answers "is the service alive on the phone", not "does
        this process hold a session". ``tunnel`` says which: ``session`` when this
        process has attached, ``probe`` when the answer came from a one-off stream.
        """
        bundled = self.bundled
        installed = self.installed_version(serial)
        enabled = self.is_service_enabled(serial) if installed is not None else False
        session = self.session(serial)
        tunnel: str | None = "session" if session is not None else None
        ping = None
        if installed is not None and enabled:
            ping = self.ping(serial)
            tunnel = tunnel or "probe"
        return {
            "package": PACKAGE_NAME,
            "installed": installed is not None,
            "installed_version": installed,
            "bundled_version": bundled.version_code if bundled else None,
            "bundled_apk_present": bundled is not None,
            "outdated": bool(
                bundled and installed is not None and installed < bundled.version_code
            ),
            "newer_than_bundled": bool(
                bundled and installed is not None and installed > bundled.version_code
            ),
            "enabled": enabled,
            "tunnel": tunnel,
            "reachable": ping is not None,
            "reported_version": ping.get("version_code") if ping else None,
            "protocol_version": ping.get("protocol_version", 1) if ping else None,
            "protocol_supported": bool(
                ping and int(ping.get("protocol_version", 1)) >= MIN_PROTOCOL_VERSION
            ),
            "token_set": bool(ping.get("token_set")) if ping else None,
            "auto_install": self._auto_install(),
            "transport_id": self.transport_id(serial),
            "session": session.to_dict() if session else None,
        }

    # ------------------------------------------------------------------ #
    # Provision: install / upgrade / enable (persistent device state)
    # ------------------------------------------------------------------ #

    def _provision_mutex(self, serial: str):
        """Serialise installs across Artemis processes sharing one device.

        A device is an (adb endpoint, serial) pair: the same serial behind two servers is
        two phones and must not share a mutex. The local default server keeps the bare
        serial key, so processes from before endpoint scoping still exclude each other.
        """
        endpoint = self.transport.endpoint
        key = serial if endpoint.is_local_default else f"{endpoint.identity}/{serial}"
        return self._file_mutex(key, f"the accessibility helper on {serial}")

    @contextmanager
    def _file_mutex(self, key: str, label: str):
        """Cross-process mutex file named by a hash of ``key``."""
        root = get_temp_dir("helper-provision")
        root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        path = root / f"{digest}.mutex"
        started = time.monotonic()
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileExistsError, PermissionError) as exc:
                # Windows may report access denied while a lock file is being deleted.
                if isinstance(exc, PermissionError) and os.name != "nt":
                    raise
                if time.monotonic() - started > _PROVISION_MUTEX_TIMEOUT_SECONDS:
                    raise TimeoutError(f"Timed out waiting for the lock on {label}.") from exc
                try:
                    age = time.time() - path.stat().st_mtime
                except OSError:
                    self._sleep(0.1)
                    continue
                if age > _PROVISION_MUTEX_STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
                if time.monotonic() - started > _PROVISION_MUTEX_TIMEOUT_SECONDS:
                    raise TimeoutError(
                        f"Timed out waiting for another process to finish with {label}."
                    )
                self._sleep(0.1)
            else:
                try:
                    os.write(fd, str(os.getpid()).encode("ascii"))
                finally:
                    os.close(fd)
                break
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def _enable_service(self, serial: str) -> bool:
        """Add the service to secure settings and confirm the write stuck.

        Right after ``pm install`` (and right after boot) AccessibilityManager
        may still not resolve the component and prunes it from the setting
        again, so a single write followed by an immediate read can report
        success for a value that is gone a moment later. Re-read after a short
        delay and retry a few times before giving up.
        """
        for attempt in range(_ENABLE_ATTEMPTS):
            result = self._adb(
                serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"
            )
            current = (result.stdout or "").strip()
            services = [s for s in current.split(":") if s and s != "null"] if current else []
            if SERVICE_NAME in services:
                # Already enabled (the common, up-to-date case): no settle wait.
                self._adb(
                    serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1"
                )
                return True
            services.append(SERVICE_NAME)
            self._adb(
                serial,
                "shell",
                "settings",
                "put",
                "secure",
                "enabled_accessibility_services",
                ":".join(services),
            )
            self._adb(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1")
            self._sleep(_ENABLE_SETTLE_SECONDS)
            if self.is_service_enabled(serial):
                return True
            logger.debug(
                f"Accessibility service setting on {serial} did not stick "
                f"(attempt {attempt + 1}/{_ENABLE_ATTEMPTS}); retrying."
            )
        return False

    def _revive_service(self, serial: str) -> None:
        """Make AccessibilityManager rebind a service that stopped answering.

        A force-stopped (or ROM-killed) accessibility service stays dead even
        though it is still listed as enabled; removing and re-adding it in the
        secure setting is what makes the system bind it again. Persistent state
        ends up exactly as before, so this is a repair, not provisioning.
        """
        result = self._adb(
            serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"
        )
        current = (result.stdout or "").strip()
        others = [s for s in current.split(":") if s and s != "null" and s != SERVICE_NAME]
        self._adb(
            serial,
            "shell",
            "settings",
            "put",
            "secure",
            "enabled_accessibility_services",
            ":".join(others) if others else "null",
        )
        self._sleep(_REVIVE_SETTLE_SECONDS)
        self._adb(
            serial,
            "shell",
            "settings",
            "put",
            "secure",
            "enabled_accessibility_services",
            ":".join([*others, SERVICE_NAME]),
        )
        self._adb(serial, "shell", "settings", "put", "secure", "accessibility_enabled", "1")
        logger.info(f"Re-bound the accessibility helper service on {serial}.")

    def provision(
        self,
        serial: str,
        *,
        force: bool = False,
        on_event: ProvisionEvent | None = None,
        install: bool = True,
    ) -> ProvisionResult:
        """Install or upgrade the helper to the bundled version and enable the service.

        Idempotent: an up-to-date, enabled helper costs two ``adb shell`` calls.
        A device running a *newer* helper than the bundle is left alone.
        ``on_event`` is told ``installing`` / ``upgrading`` before the install.
        ``install=False`` (auto-install disabled) never runs ``adb install``: a
        missing helper is reported as a failure that names the manual command,
        an outdated one is used as is.
        """
        bundled = self.bundled

        def emit(event: str, **details: Any) -> None:
            if on_event is None:
                return
            try:
                on_event(event, {"serial": serial, **details})
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug(f"Provision event callback failed: {exc}")

        with self._provision_mutex(serial):
            installed = self.installed_version(serial)
            action = "up_to_date"
            needs_install = (
                force
                or installed is None
                or (bundled is not None and installed < bundled.version_code)
            )
            if needs_install and not install:
                if installed is None:
                    return ProvisionResult(
                        ok=False,
                        action="failed",
                        installed_version=None,
                        bundled_version=bundled.version_code if bundled else None,
                        enabled=False,
                        error=(
                            "The accessibility helper is not installed on this device and "
                            f"{ENV_ARTEMIS_HELPER_AUTO_INSTALL}=false forbids installing it "
                            f"from a task. Install it once with: artemis helper install "
                            f"--serial {serial}"
                        ),
                    )
                logger.info(
                    f"Accessibility helper on {serial} is v{installed} (bundled "
                    f"v{bundled.version_code if bundled else '?'}); not upgrading because "
                    f"{ENV_ARTEMIS_HELPER_AUTO_INSTALL}=false."
                )
                action = "skipped"
                needs_install = False
            if needs_install:
                if bundled is None:
                    if installed is None:
                        return ProvisionResult(
                            ok=False,
                            action="failed",
                            installed_version=None,
                            bundled_version=None,
                            enabled=False,
                            error=(
                                "The accessibility helper is not installed and no bundled APK "
                                f"was found at {BUNDLED_APK_PATH}."
                            ),
                        )
                    action = "skipped"
                else:
                    # This is the one moment Artemis changes what is installed on the
                    # phone; say so in a way that survives in every log.
                    logger.warning(
                        f"Installing the Artemis accessibility helper v{bundled.version_name} "
                        f"(code {bundled.version_code}) on {serial}. It is an accessibility "
                        "service that reads the screen layout for automated tests, answers only "
                        "on the phone's loopback port to a host holding the session token, and "
                        "stays installed until removed with `artemis helper uninstall`. Set "
                        f"{ENV_ARTEMIS_HELPER_AUTO_INSTALL}=false to install it by hand only."
                    )
                    emit(
                        "installing" if installed is None else "upgrading",
                        from_version=installed,
                        to_version=bundled.version_code,
                        version_name=bundled.version_name,
                    )
                    result = self._adb(serial, "install", "-r", "-g", str(bundled.apk_path))
                    output = f"{result.stdout or ''}\n{result.stderr or ''}"
                    if result.returncode != 0 or "Success" not in output:
                        return ProvisionResult(
                            ok=False,
                            action="failed",
                            installed_version=installed,
                            bundled_version=bundled.version_code,
                            enabled=False,
                            error=f"adb install failed: {output.strip()[-400:]}",
                        )
                    action = "installed" if installed is None else "upgraded"
                    installed = self.installed_version(serial)
            enabled = self._enable_service(serial)
            if not enabled:
                # Secure settings were rejected (OEM ROM or restricted settings):
                # open the Accessibility settings screen so the person only has to
                # flip the switch, and say exactly where it is.
                self._adb(serial, "shell", "am", "start", "-a", ACCESSIBILITY_SETTINGS_INTENT)
        return ProvisionResult(
            ok=enabled,
            action=action,
            installed_version=installed,
            bundled_version=bundled.version_code if bundled else None,
            enabled=enabled,
            error=None
            if enabled
            else (
                "This device rejected enabling the accessibility service from adb. "
                f"The Accessibility settings screen was opened on {serial}; enable it by hand: "
                f"{MANUAL_ENABLE_PATH}."
            ),
        )

    def uninstall(self, serial: str) -> bool:
        """Remove the helper from the device (never done automatically)."""
        self.detach(serial)
        result = self._adb(
            serial, "shell", "settings", "get", "secure", "enabled_accessibility_services"
        )
        current = (result.stdout or "").strip()
        services = [s for s in current.split(":") if s and s != "null" and s != SERVICE_NAME]
        self._adb(
            serial,
            "shell",
            "settings",
            "put",
            "secure",
            "enabled_accessibility_services",
            ":".join(services) if services else "null",
        )
        result = self._adb(serial, "uninstall", PACKAGE_NAME)
        return result.returncode == 0 and "Success" in (result.stdout or "")

    # ------------------------------------------------------------------ #
    # Attach / detach (host-side tunnel)
    # ------------------------------------------------------------------ #

    def _wait_for_ping(self, serial: str, attempts: int) -> dict[str, Any] | None:
        for attempt in range(attempts):
            info = self.ping(serial)
            if info is not None:
                return info
            if attempt + 1 < attempts:
                self._sleep(_PING_INTERVAL_SECONDS)
        return None

    def attach(
        self,
        serial: str,
        *,
        provision: bool = True,
        on_event: ProvisionEvent | None = None,
        revive: bool | None = None,
    ) -> HelperSession:
        """Return a live session for ``serial``, building the tunnel when needed.

        ``provision=True`` (task path) installs / upgrades / enables first.
        ``provision=False`` (observers) only connects to a helper that is
        already running and raises :class:`HelperUnavailable` otherwise.
        ``revive`` (defaults to ``provision``) allows re-binding a dead but
        enabled service; :meth:`reattach` always does, because the caller
        already owned a working session on this device.
        """
        if revive is None:
            revive = provision
        with self._device_lock(serial):
            transport = self.transport_id(serial)
            existing = self.session(serial)
            if existing is not None:
                if existing.transport_id == transport and self.ping(serial):
                    return existing
                logger.info(
                    f"Accessibility helper session on {serial} is stale "
                    f"(transport {existing.transport_id} -> {transport}); re-checking the helper."
                )
                self._drop_session(serial)

            if provision:
                result = self.provision(serial, on_event=on_event, install=self._auto_install())
                if not result.ok:
                    raise HelperUnavailable(result.error or "Provisioning the helper failed.")

            attempts = _PING_ATTEMPTS_AFTER_PROVISION if provision else _PING_ATTEMPTS_LAZY
            info = self._wait_for_ping(serial, attempts)
            suppressed_by: str | None = None
            if info is None and self.installed_version(serial) is not None:
                # Silent although installed. The usual cause is a UiAutomation
                # holder (uiautomator2 / Appium) that made Android unbind every
                # accessibility service; only then a killed service.
                holders = self.uiautomation_holders(serial)
                if holders["uiautomator2"] and revive:
                    self.stop_uiautomator2_server(serial, holders["uiautomator2"])
                    info = self._wait_for_ping(serial, _PING_ATTEMPTS_AFTER_PROVISION)
                elif holders["uiautomator2"]:
                    suppressed_by = "uiautomator2's device server"
                if info is None and holders["appium"]:
                    suppressed_by = "Appium's UiAutomator2 server"
                if info is None and revive and suppressed_by is None:
                    # Installed and enabled but silent: the service was killed
                    # (force-stop, ROM task killer) or has not bound yet after an
                    # upgrade. Re-binding it is cheap and needs no install.
                    if self.is_service_enabled(serial):
                        self._revive_service(serial)
                    else:
                        self._enable_service(serial)
                    info = self._wait_for_ping(serial, _PING_ATTEMPTS_AFTER_PROVISION)
            if info is None:
                if suppressed_by:
                    raise HelperUnavailable(
                        f"The accessibility helper on {serial} is suppressed by {suppressed_by}: "
                        "Android unbinds accessibility services while a UiAutomation connection "
                        "without FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES is alive. Stop that "
                        "server (for Appium, set the disableSuppressAccessibilityServices "
                        "capability) or run with ARTEMIS_HIERARCHY_BACKEND=uiautomator."
                    )
                raise HelperUnavailable(
                    f"The accessibility helper on {serial} is not answering on "
                    f"device port {DEVICE_PORT} via {self.transport.endpoint.identity}."
                )

            protocol = _protocol_of(info)
            if protocol < MIN_PROTOCOL_VERSION:
                # A helper older than this host can talk to (a dev build with a
                # newer versionCode but the old contract, or a downgrade). Reinstall
                # the bundle when this caller may, otherwise say what to run.
                bundled = self.bundled
                if provision and self._auto_install() and bundled is not None:
                    logger.warning(
                        f"Accessibility helper on {serial} speaks protocol {protocol} "
                        f"(host needs >= {MIN_PROTOCOL_VERSION}); reinstalling the bundled "
                        f"v{bundled.version_name}."
                    )
                    result = self.provision(serial, force=True, on_event=on_event)
                    if not result.ok:
                        raise HelperUnavailable(result.error or "Reinstalling the helper failed.")
                    info = self._wait_for_ping(serial, _PING_ATTEMPTS_AFTER_PROVISION)
                    protocol = _protocol_of(info) if info else 0
                if protocol < MIN_PROTOCOL_VERSION:
                    raise HelperUnavailable(
                        f"The accessibility helper on {serial} speaks protocol {protocol}, "
                        f"this host needs {MIN_PROTOCOL_VERSION} or newer. Reinstall it with: "
                        f"artemis helper install --serial {serial} --force"
                    )

            token: str | None = None
            if info.get("auth_required", protocol >= 2):
                token = self.host_token()
                self.push_token(serial)

            version = info.get("version_code")
            session = HelperSession(
                serial=serial,
                transport_id=transport,
                version_code=int(version) if isinstance(version, (int, float)) else None,
                version_name=str(info.get("version_name") or ""),
                endpoint=self.transport.endpoint.identity,
                token=token,
                protocol_version=protocol,
            )
            with self._registry_lock:
                self._sessions[self._key(serial)] = session
            logger.info(
                f"Accessibility helper attached on {serial} via {session.endpoint} "
                f"(v{session.version_name or '?'}, protocol {protocol}, transport {transport})"
            )
            return session

    def reattach(self, serial: str) -> HelperSession:
        """Re-check the helper after a request failed; never installs anything.

        The caller had a working session, so a dead service is re-bound.
        """
        with self._device_lock(serial):
            self._drop_session(serial)
            return self.attach(serial, provision=False, revive=True)

    def detach(self, serial: str) -> None:
        with self._device_lock(serial):
            self._drop_session(serial)

    def _drop_session(self, serial: str) -> None:
        with self._registry_lock:
            self._sessions.pop(self._key(serial), None)

    def detach_all(self) -> None:
        with self._registry_lock:
            self._sessions.clear()


def _protocol_of(info: dict[str, Any] | None) -> int:
    try:
        return int((info or {}).get("protocol_version", 1))
    except (TypeError, ValueError):
        return 1


helper_manager = AccessibilityHelperManager()

_managers: dict[AdbEndpoint, AccessibilityHelperManager] = {}
_managers_lock = threading.Lock()


def helper_manager_for(endpoint: AdbEndpoint | None) -> AccessibilityHelperManager:
    """The manager bound to ``endpoint`` (one per endpoint); the process-wide one for ``None``."""
    if endpoint is None:
        return helper_manager
    with _managers_lock:
        manager = _managers.get(endpoint)
        if manager is None:
            manager = _managers[endpoint] = AccessibilityHelperManager(
                transport=EndpointTransport.shared(endpoint)
            )
        return manager


__all__ = [
    "AccessibilityHelperManager",
    "BundledHelper",
    "DEVICE_PORT",
    "HelperSession",
    "HelperUnavailable",
    "MANUAL_ENABLE_PATH",
    "MIN_PROTOCOL_VERSION",
    "PACKAGE_NAME",
    "ProvisionEvent",
    "ProvisionResult",
    "SERVICE_NAME",
    "TOKEN_ACTION",
    "TOKEN_RECEIVER",
    "auto_install_allowed",
    "helper_manager",
    "helper_manager_for",
    "load_bundled_helper",
]
