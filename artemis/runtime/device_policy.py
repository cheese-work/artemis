"""Restricted device command policy, audit log and ``left_test_app`` stop (CHE-2129, S3.2b).

A guest phone that holds its owner's personal data (the X99 Xiaomi) is *restricted*:
every adb command aimed at its serial passes one gate, :func:`authorize`, which
``EndpointTransport`` calls before it builds a subprocess. SmartQA's own commands and the
LLM agent's device tools both go through the transport, so one gate covers both.

The gate allows an exact allowlist and refuses everything else with
``device_policy_denied`` *before* any process or socket exists. Each command, allowed or
refused, lands in an :class:`AuditLog` (time, serial, caller, command, decision). A refused
command is logged as its verb only (``shell pm``): its arguments may be personal data.

Off unless ``ARTEMIS_DEVICE_POLICY`` is enabled: with it off :func:`restrict` does nothing
and the registry stays empty, so the transport behaves exactly as before.

The allowlist, as argv after ``-s <serial>``:

* ``install [-r] <apk>`` for a listed APK path; ``uninstall <pkg>`` for a test package
* ``shell am start [-W] -n <test pkg>/<activity>``
* ``shell dumpsys activity activities`` (the foreground check; its output is parsed, never kept)
* ``shell|exec-out screencap -p``, only as the command right after a foreground check
* ``shell logcat [-b main|crash] -d --uid <test uid>``
* ``shell cmd uimode night [yes|no|auto]``; ``shell settings get|put global`` for the three
  animation scales and ``stay_on_while_plugged_in``

Not covered by design: ``open_stream`` (a raw stream to an on-device service, no shell).
``EndpointTransport.device`` and ``u2_connect`` hand out an arbitrary shell, so they are
refused for a restricted serial.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

ENV_DEVICE_POLICY = "ARTEMIS_DEVICE_POLICY"
_TRUTHY = frozenset({"enabled", "1", "true", "yes", "on"})

_TOKEN = re.compile(r"[\w./:=@+-]+")
_SHELL_LINE = re.compile(
    r"[\w./:=@+-]+( [\w./:=@+-]+)*"
)  # single spaces only: no newline, no ; | & $
_COMPONENT = re.compile(r"([\w.]+)/[\w.]+")
_RESUMED = re.compile(r"ResumedActivity.*? u\d+ ([\w.]+)/")
_NUMBER = re.compile(r"\d+(\.\d+)?")
_ANIMATION_SCALES = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)
_SETTINGS = {
    **dict.fromkeys(_ANIMATION_SCALES, (_NUMBER, "1.0")),
    "stay_on_while_plugged_in": (re.compile(r"[0-7]"), "0"),
}
_NIGHT_MODES = frozenset({"yes", "no", "auto"})
_HOST_VERBS = frozenset({"devices", "version", "start-server"})  # name no device

_caller: ContextVar[str] = ContextVar("device_policy_caller", default="smartqa")


def enabled() -> bool:
    return os.environ.get(ENV_DEVICE_POLICY, "").strip().lower() in _TRUTHY


class DevicePolicyDenied(Exception):
    code = "device_policy_denied"


class LeftTestApp(Exception):
    """The foreground package is not the run's test app; the run stops."""

    code = "left_test_app"


@contextmanager
def as_caller(name: str) -> Iterator[None]:
    """Label the commands sent inside the block (``smartqa`` by default, e.g. ``llm_agent``)."""
    token = _caller.set(name)
    try:
        yield
    finally:
        _caller.reset(token)


class AuditLog:
    """Every command decision, in order; optionally appended to a JSONL file as it happens."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, **entry: Any) -> None:
        entry = {"time": datetime.now(UTC).isoformat(), **entry}
        with self._lock:
            self._entries.append(entry)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries)

    def receipt(self, run_id: str) -> dict[str, Any]:
        """The run's command receipt: its entries, JSON-serialisable."""
        return {"run_id": run_id, "entries": [e for e in self.entries() if e["run_id"] == run_id]}


def _withheld(argv: Sequence[str]) -> str:
    """The verb of a refused command (``shell pm``); arguments could carry personal data."""
    words = [w for a in argv[:2] for w in a.split()[:1]]
    verb = words[:2] if words[:1] == ["shell"] else words[:1]
    return " ".join(w if re.fullmatch(r"[\w-]{1,24}", w) else "?" for w in verb)


class RestrictedDevice:
    def __init__(
        self,
        transport: Any,
        serial: str,
        *,
        test_packages: frozenset[str],
        test_uid: int,
        locked_package: str | None,
        apk_paths: frozenset[str],
        run_id: str,
        audit: AuditLog,
    ) -> None:
        self.transport = transport
        self.serial = serial
        self.test_packages = test_packages
        self.test_uid = test_uid
        self.apk_paths = apk_paths
        self.run_id = run_id
        self.audit = audit
        self._foreground = frozenset({locked_package}) if locked_package else test_packages
        self._after_foreground_check = False
        self._saved: dict[str, str] = {}
        self._lock = threading.Lock()

    # -- the gate ------------------------------------------------------------

    def record(self, command: str, decision: str, serial: str | None = None) -> None:
        self.audit.record(
            serial=serial or self.serial,
            caller=_caller.get(),
            command=command,
            decision=decision,
            run_id=self.run_id,
        )

    def authorize(self, argv: Sequence[str]) -> None:
        with self._lock:
            after_check, self._after_foreground_check = self._after_foreground_check, False
            command = self._match(list(argv), after_check)
        if command is None:
            self.record(_withheld(argv), "denied")
            raise DevicePolicyDenied(f"device_policy_denied: {self.serial}")
        self.record(command, "allowed")

    def _match(self, argv: list[str], after_check: bool) -> str | None:
        """The canonical (loggable) command when ``argv`` is on the allowlist, else ``None``."""
        if not argv:
            return None
        verb, rest = argv[0], argv[1:]
        if verb == "shell":
            line = " ".join(rest)
            if not _SHELL_LINE.fullmatch(line):
                return None
            tokens = line.split()
            return f"shell {line}" if self._shell(tokens, after_check) else None
        if not all(_TOKEN.fullmatch(a) for a in rest):
            return None
        if verb == "exec-out":
            return "exec-out screencap -p" if rest == ["screencap", "-p"] and after_check else None
        if verb == "install" and rest and rest[-1] in self.apk_paths and set(rest[:-1]) <= {"-r"}:
            return f"install {Path(rest[-1]).name}"
        if verb == "uninstall" and len(rest) == 1 and rest[0] in self.test_packages:
            return f"uninstall {rest[0]}"
        return None

    def _shell(self, t: list[str], after_check: bool) -> bool:
        if t == ["screencap", "-p"]:
            return after_check
        if t == ["dumpsys", "activity", "activities"]:
            return True
        if t[:3] == ["cmd", "uimode", "night"]:
            return len(t) == 3 or (len(t) == 4 and t[3] in _NIGHT_MODES)
        if t[:3] in (["settings", "get", "global"], ["settings", "put", "global"]):
            put = t[1] == "put"
            if len(t) != 4 + put or t[3] not in _SETTINGS:
                return False
            return not put or bool(_SETTINGS[t[3]][0].fullmatch(t[4]))
        if t[:2] == ["am", "start"]:
            options = t[2:-1]
            component = _COMPONENT.fullmatch(t[-1]) if len(t) > 2 else None
            return (
                options in (["-n"], ["-W", "-n"])
                and component is not None
                and component.group(1) in self.test_packages
            )
        if t[:1] == ["logcat"]:
            return self._logcat(t[1:])
        return False

    def _logcat(self, t: list[str]) -> bool:
        seen: set[str] = set()
        i = 0
        while i < len(t):
            flag, arg = t[i], t[i + 1] if i + 1 < len(t) else None
            if flag == "-d":
                seen.add("d")
                i += 1
                continue
            if (flag, arg) == ("--uid", str(self.test_uid)):
                seen.add("uid")
            elif not (
                (flag == "-b" and arg in ("main", "crash")) or (flag, arg) == ("-v", "threadtime")
            ):
                return False
            i += 2
        return seen == {"d", "uid"}

    # -- commands the run sends through the gate -------------------------------

    def _run(self, argv: list[str]) -> bytes:
        done = self.transport.run(["-s", self.serial, *argv], capture_output=True)
        done.check_returncode()
        out = done.stdout or b""
        return out.encode() if isinstance(out, str) else out

    def check_foreground(self) -> None:
        """Raise :class:`LeftTestApp` unless the test app is in front; arms the next screencap."""
        text = self._run(["shell", "dumpsys", "activity", "activities"]).decode(errors="replace")
        match = _RESUMED.search(text)
        if match is None or match.group(1) not in self._foreground:  # unreadable counts as left
            raise LeftTestApp(f"left_test_app: {self.serial}")
        with self._lock:
            self._after_foreground_check = True

    def capture_screenshot(self, dest: Path) -> None:
        """Write a screenshot only if the test app was in front before and after the capture."""
        self.check_foreground()
        png = self._run(["exec-out", "screencap", "-p"])
        self.check_foreground()  # the app may have left while the frame was taken: discard it
        dest.write_bytes(png)

    def apply_setting(self, key: str, value: str) -> None:
        """Set a device setting (or ``night_mode``), remembering the first value for restore."""
        if key not in self._saved:
            self._saved[key] = self._get(key)
        self._put(key, value)

    def restore_settings(self) -> None:
        for key in reversed(list(self._saved)):
            self._put(key, self._saved[key])
        self._saved.clear()

    def _get(self, key: str) -> str:
        if key == "night_mode":
            out = self._run(["shell", "cmd", "uimode", "night"]).decode().split()
            return out[-1] if out else "auto"
        out = self._run(["shell", "settings", "get", "global", key]).decode().strip()
        return _SETTINGS[key][1] if out in ("", "null") else out

    def _put(self, key: str, value: str) -> None:
        if key == "night_mode":
            self._run(["shell", "cmd", "uimode", "night", value])
        else:
            self._run(["shell", "settings", "put", "global", key, value])


# -- registry and the transport's entry points ---------------------------------

_DEVICES: dict[str, RestrictedDevice] = {}
_REGISTRY_LOCK = threading.Lock()


def restrict(
    transport: Any,
    serial: str,
    *,
    test_packages: frozenset[str],
    test_uid: int,
    locked_package: str | None = None,
    apk_paths: frozenset[str] = frozenset(),
    run_id: str = "",
    audit: AuditLog | None = None,
) -> RestrictedDevice | None:
    """Restrict ``serial`` for a run (call it when a restricted lease is taken); a no-op when off.

    ``locked_package`` is the run's test app; the foreground must be it. Without one, any
    of ``test_packages`` may be in front.
    """
    if not enabled():
        return None
    device = RestrictedDevice(
        transport,
        serial,
        test_packages=test_packages,
        test_uid=test_uid,
        locked_package=locked_package,
        apk_paths=apk_paths,
        run_id=run_id,
        audit=audit or AuditLog(),
    )
    with _REGISTRY_LOCK:
        _DEVICES[serial] = device
    return device


def unrestrict(serial: str) -> None:
    with _REGISTRY_LOCK:
        _DEVICES.pop(serial, None)


def reset() -> None:
    with _REGISTRY_LOCK:
        _DEVICES.clear()


def authorize(arguments: Sequence[str]) -> None:
    """The one gate: called by ``EndpointTransport`` with the adb arguments, before it spawns."""
    if not _DEVICES:
        return
    args = list(arguments)
    if args[:1] == ["-s"] and len(args) >= 2:
        device = _DEVICES.get(args[1])
        if device is not None:
            device.authorize(args[2:])
        return
    if args[:1] and args[0] in _HOST_VERBS:
        return
    # Names no serial, so it could reach a restricted phone: refuse it for every restricted run.
    for device in list(_DEVICES.values()):
        device.record(_withheld(args), "denied", serial="-")
    raise DevicePolicyDenied("device_policy_denied: command names no serial")


def refuse_raw_access(serial: str) -> None:
    """adbutils / uiautomator2 give an arbitrary shell: refuse them for a restricted serial."""
    device = _DEVICES.get(serial)
    if device is not None:
        device.record("adbutils", "denied")
        raise DevicePolicyDenied(f"device_policy_denied: {serial}")
