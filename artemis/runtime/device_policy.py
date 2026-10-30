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

The command line must name exactly one ``-s <serial>`` and no other adb selector or endpoint
option. The allowlist, as argv after ``-s <serial>``:

* ``install [-r] <apk>`` for a listed APK path; ``uninstall <pkg>`` for a test package
* ``shell am start [-W] -n <test pkg>/<activity>``
* ``shell dumpsys activity activities`` (the foreground check; its output is parsed, never kept)
* ``shell logcat [-b main|crash] -d --uid <test uid>``
* ``shell cmd uimode night`` and ``shell settings get global <key>`` (reads)

Two kinds of command are never on the list. They run only through a method of
:class:`RestrictedDevice`, which grants the gate a permit for that exact argv, once, to the
calling thread:

* ``capture_screenshot`` (foreground check, ``exec-out screencap -p``, foreground check)
* ``apply_setting`` (a tracked ``settings put`` or ``cmd uimode night <mode>``), restored by
  :func:`finish_run`

adbutils and uiautomator2 reach the phone without a subprocess, so :func:`guard_connection`
wraps every adb-server connection ``EndpointTransport.client`` hands out: a restricted serial,
or any transport selection that cannot be resolved to a serial, is refused. The accessibility
helper is the one supported way to observe and tap; :func:`helper_call` gates each request
(route allowlist, foreground check before and after, audit) and opens the one
``tcp:`` stream it needs. :func:`activate_for_run` and :func:`finish_run` tie all of this to a
run: a restricted lease (or ``ARTEMIS_RESTRICTED_SERIALS``) turns the policy on for its serial.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
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
_OPTIONS_WITH_VALUE = frozenset({"-s", "-t", "-H", "-P", "-L"})  # adb's global options
_HELPER_ROUTES = frozenset(
    {
        ("GET", "/ping"),
        ("GET", "/snapshot"),
        ("GET", "/dump_xml"),
        ("GET", "/dump"),
        ("POST", "/action"),
    }
)
#: Loopback port the accessibility helper binds on the device: the only service a helper stream may reach.
HELPER_PORT = 18888
#: ``/action`` commands a run needs. Clipboard, ``global`` (but ``back``) and the rest are refused.
_ACTION_COMMANDS = frozenset({"tap", "double_tap", "long_press", "swipe", "type", "clear"})
_SERVER_HOST_COMMANDS = (  # adb-server commands that select no device
    "host:version",
    "host:devices",
    "host:track-devices",
    "host:connect:",
    "host:disconnect:",
    "host:list-forward",
)
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
_helper_serial: ContextVar[str | None] = ContextVar("device_policy_helper", default=None)


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
        test_uid: int | None,
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
        self.stopped = False  # latched by the first LeftTestApp: nothing more is observed
        self._foreground = frozenset({locked_package}) if locked_package else test_packages
        self._saved: dict[str, str] = {}
        self._permit: tuple[int, tuple[str, ...], str] | None = None
        self._slot = threading.Lock()  # guards ``_permit``
        self._operation = threading.RLock()  # one permitted operation at a time

    def bind_package(self, package: str) -> None:
        """Make ``package`` the run's test app: the only foreground the run may keep."""
        self.test_packages = self.test_packages | {package}
        self._foreground = frozenset({package})

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
        with self._slot:
            granted, key = self._permit, (threading.get_ident(), tuple(argv))
            if granted is not None and granted[:2] == key:
                self._permit = None  # single use
                command = granted[2]
            elif self.stopped:  # a left-app stop ends the run's commands; only a permit passes
                self.record(_withheld(argv), "denied")
                raise LeftTestApp(f"left_test_app: {self.serial}")
            else:
                command = self._match(list(argv))
        if command is None:
            self.record(_withheld(argv), "denied")
            raise DevicePolicyDenied(f"device_policy_denied: {self.serial}")
        self.record(command, "allowed")

    def _match(self, argv: list[str]) -> str | None:
        """The canonical (loggable) command when ``argv`` is on the allowlist, else ``None``."""
        if not argv:
            return None
        verb, rest = argv[0], argv[1:]
        if verb == "shell":
            line = " ".join(rest)
            if not _SHELL_LINE.fullmatch(line):
                return None
            return f"shell {line}" if self._shell(line.split()) else None
        if not all(_TOKEN.fullmatch(a) for a in rest):
            return None
        if verb == "install" and rest and rest[-1] in self.apk_paths and set(rest[:-1]) <= {"-r"}:
            return f"install {Path(rest[-1]).name}"
        if verb == "uninstall" and len(rest) == 1 and rest[0] in self.test_packages:
            return f"uninstall {rest[0]}"
        return None

    def _shell(self, t: list[str]) -> bool:
        if t == ["dumpsys", "activity", "activities"] or t == ["cmd", "uimode", "night"]:
            return True
        if t[:3] == ["settings", "get", "global"]:
            return len(t) == 4 and t[3] in _SETTINGS
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
        if self.test_uid is None:
            return False
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

    def _run(self, argv: list[str], *, permit: str | None = None) -> bytes:
        """Send ``argv`` through the transport; ``permit`` is the canonical name of a command
        that is not on the allowlist and runs once, for this thread and this exact argv."""
        with self._operation:
            if permit is not None:
                with self._slot:
                    self._permit = (threading.get_ident(), tuple(argv), permit)
            try:
                done = self.transport.run(["-s", self.serial, *argv], capture_output=True)
            finally:
                with self._slot:
                    self._permit = None  # never left behind, whatever the gate or adb did
        done.check_returncode()
        out = done.stdout or b""
        return out.encode() if isinstance(out, str) else out

    def check_foreground(self) -> None:
        """Raise :class:`LeftTestApp` unless the test app is in front; the stop is latched."""
        if self.stopped:
            raise LeftTestApp(f"left_test_app: {self.serial}")
        text = self._run(["shell", "dumpsys", "activity", "activities"]).decode(errors="replace")
        match = _RESUMED.search(text)
        if match is None or match.group(1) not in self._foreground:  # unreadable counts as left
            self.stopped = True
            raise LeftTestApp(f"left_test_app: {self.serial}")

    def capture_screenshot(self, dest: Path) -> None:
        """Write a screenshot only if the test app was in front before and after the capture."""
        with self._operation:
            self.check_foreground()
            png = self._run(["exec-out", "screencap", "-p"], permit="exec-out screencap -p")
            self.check_foreground()  # the app may have left while the frame was taken: discard it
            dest.write_bytes(png)

    def helper(
        self,
        method: str,
        path: str,
        call: Callable[[], bytes],
        *,
        port: int,
        body: bytes | None = None,
    ) -> bytes:
        """One accessibility-helper request: port, route and command allowlist, foreground checks, audit.

        The payload is withheld (``LeftTestApp``) when the test app was not in front
        before the request or is not in front after it.
        """
        if self.stopped:
            raise LeftTestApp(f"left_test_app: {self.serial}")
        route = (method, path.split("?", 1)[0])
        action = _action_command(body) if route[1] == "/action" else None
        if (
            port != HELPER_PORT
            or route not in _HELPER_ROUTES
            or (route[1] == "/action" and action is None)
        ):
            self.record("helper", "denied")
            raise DevicePolicyDenied(f"device_policy_denied: {self.serial}")
        observed = route[1] != "/ping"
        if observed:
            self.check_foreground()
        self.record(" ".join(("helper", *route, *filter(None, [action]))), "allowed")
        token = _helper_serial.set(self.serial)
        try:
            payload = call()
        finally:
            _helper_serial.reset(token)
        if observed:
            self.check_foreground()
        return payload

    def apply_setting(self, key: str, value: str) -> None:
        """Set a device setting (or ``night_mode``), remembering the first value for restore."""
        self._check_setting(key, value)
        if self.stopped:
            raise LeftTestApp(f"left_test_app: {self.serial}")
        with self._operation:
            if key not in self._saved:
                self._saved[key] = self._get(key)
            self._put(key, value)

    def restore_settings(self) -> None:
        """Put every tracked setting back; one that fails stays tracked and is raised last."""
        failure: Exception | None = None
        with self._operation:
            for key in reversed(list(self._saved)):
                try:
                    self._put(key, self._saved[key])
                    del self._saved[key]
                except (OSError, subprocess.SubprocessError, DevicePolicyDenied) as exc:
                    # keep restoring the others; the first failure is raised last
                    failure = failure or exc
        if failure is not None:
            raise failure

    @staticmethod
    def _check_setting(key: str, value: str) -> None:
        valid = (
            value in _NIGHT_MODES
            if key == "night_mode"
            else (key in _SETTINGS and bool(_SETTINGS[key][0].fullmatch(value)))
        )
        if not valid:
            raise DevicePolicyDenied(f"device_policy_denied: setting {key}")

    def _get(self, key: str) -> str:
        if key == "night_mode":
            out = self._run(["shell", "cmd", "uimode", "night"]).decode().split()
            return out[-1] if out else "auto"
        out = self._run(["shell", "settings", "get", "global", key]).decode().strip()
        return _SETTINGS[key][1] if out in ("", "null") else out

    def _put(self, key: str, value: str) -> None:
        argv = (
            ["shell", "cmd", "uimode", "night", value]
            if key == "night_mode"
            else ["shell", "settings", "put", "global", key, value]
        )
        self._run(argv, permit=" ".join(argv))


# -- registry and the transport's entry points ---------------------------------

_DEVICES: dict[str, RestrictedDevice] = {}
_REGISTRY_LOCK = threading.Lock()


def restrict(
    transport: Any,
    serial: str,
    *,
    test_packages: frozenset[str],
    test_uid: int | None,
    locked_package: str | None = None,
    apk_paths: frozenset[str] = frozenset(),
    run_id: str = "",
    audit: AuditLog | None = None,
) -> RestrictedDevice | None:
    """Restrict ``serial`` for a run; a no-op when off. :func:`activate_for_run` is the caller.

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


def _deny_all(command: str, why: str) -> None:
    """Refuse a command that could reach any restricted phone, and log it for each."""
    for device in list(_DEVICES.values()):
        device.record(command, "denied", serial="-")
    raise DevicePolicyDenied(f"device_policy_denied: {why}")


def authorize(arguments: Sequence[str]) -> None:
    """The one gate: called by ``EndpointTransport`` with the adb arguments, before it spawns."""
    if not _DEVICES:
        return
    args = list(arguments)
    i = 0
    while i < len(args) and args[i].startswith("-"):  # adb's global options, whatever they are
        i += 2 if args[i] in _OPTIONS_WITH_VALUE else 1
    options, rest = args[:i], args[i:]
    if len(options) == 2 and options[0] == "-s":
        device = _DEVICES.get(options[1])
        if device is not None:
            device.authorize(rest)
        return
    if not options and rest[:1] and rest[0] in _HOST_VERBS:
        return
    # Names no single serial, so it could reach a restricted phone: refuse it for every run.
    _deny_all(_withheld(rest), "command does not name exactly one serial")


def refuse_raw_access(serial: str) -> None:
    """adbutils / uiautomator2 give an arbitrary shell: refuse them for a restricted serial."""
    device = _DEVICES.get(serial)
    if device is not None:
        device.record("adbutils", "denied")
        raise DevicePolicyDenied(f"device_policy_denied: {serial}")


# -- adbutils: every adb-server connection ----------------------------------------


def guard_connection(connection: Any) -> Any:
    """Make ``connection.send_command`` refuse what would reach a restricted phone."""
    state = {"tcp_only": False}
    send = connection.send_command

    def send_command(command: str) -> None:
        if _DEVICES:
            _check_server_command(command, state)
        send(command)

    connection.send_command = send_command
    return connection


def _check_server_command(command: str, state: dict[str, bool]) -> None:
    if state["tcp_only"]:  # a helper stream: one tcp: service, nothing else
        if command != f"tcp:{HELPER_PORT}":
            _deny_all("adbutils", "only the helper stream may use this connection")
        return
    for serial, device in list(_DEVICES.items()):
        selects = command in (f"host:transport:{serial}", f"host:tport:serial:{serial}")
        if selects and _helper_serial.get() == serial:
            state["tcp_only"] = True
            return
        if selects or command.startswith(f"host-serial:{serial}:"):
            device.record("adbutils", "denied")
            raise DevicePolicyDenied(f"device_policy_denied: {serial}")
    if command.startswith(("host:transport:", "host:tport:serial:", "host-serial:")):
        return  # names a serial that is not restricted
    if command.startswith("host") and not command.startswith(_SERVER_HOST_COMMANDS):
        # transport-id / any / usb / local and the like cannot be resolved to a serial here
        _deny_all("adbutils", "cannot tell which phone this selects")


# -- the accessibility helper --------------------------------------------------


def _action_command(body: bytes | None) -> str | None:
    """The name of the allowed ``/action`` command in ``body`` (``global back`` too), else ``None``."""
    try:
        data = json.loads(body or b"")
    except (ValueError, RecursionError):
        return None
    cmd = data.get("cmd") if isinstance(data, dict) else None
    if isinstance(cmd, str) and cmd in _ACTION_COMMANDS:
        return cmd
    return "global back" if cmd == "global" and data.get("action") == "back" else None


def helper_call(
    serial: str,
    method: str,
    path: str,
    call: Callable[[], bytes],
    *,
    port: int,
    body: bytes | None = None,
) -> bytes:
    """Run one helper request ``call`` under the policy of ``serial`` (none: just run it)."""
    device = _DEVICES.get(serial)
    return call() if device is None else device.helper(method, path, call, port=port, body=body)


# -- run lifecycle -------------------------------------------------------------

ENV_RESTRICTED_SERIALS = "ARTEMIS_RESTRICTED_SERIALS"


def lease_db_path() -> Path:
    from artemis.config import DB_PATH

    return DB_PATH


def _restricted_by_lease(serial: str) -> bool:
    """Is a live restricted lease held on the phone that has this serial?"""
    path = lease_db_path()
    if not path.exists():
        return False
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM device_leases l JOIN device_connections c"
                " ON c.device_id = l.device_id"
                " WHERE c.serial = ? AND l.restricted = 1 AND l.expires_at > ?",
                (serial, time.time()),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):  # leases were never used on this machine
                return False
            raise  # unreadable: the caller must not run unrestricted
    return row is not None


def is_restricted(serial: str) -> bool:
    listed = os.environ.get(ENV_RESTRICTED_SERIALS, "").split(",")
    return serial in (s.strip() for s in listed) or _restricted_by_lease(serial)


def may_describe(serial: str) -> bool:
    """May discovery run a command on ``serial``? ``False`` for a restricted or unknown phone.

    Automatic selection lists phones before any run (and so any policy) exists; a restricted
    phone is then picked from the host-only listing and described by nothing it is asked.
    """
    if serial in _DEVICES:
        return False
    if not enabled():
        return True
    try:
        return not is_restricted(serial)
    except (sqlite3.Error, OSError):  # an unreadable lease store must not read as "unrestricted"
        return False


def activate_for_run(
    transport: Any,
    serial: str,
    *,
    run_id: str,
    audit: AuditLog | None = None,
) -> RestrictedDevice | None:
    """Turn the policy on for ``serial`` if it is restricted (flag on, live lease or env list).

    Raises when the lease store cannot be read: an unknown phone is not run unrestricted.
    """
    if not enabled() or not is_restricted(serial):
        return None
    return restrict(
        transport, serial, test_packages=frozenset(), test_uid=None, run_id=run_id, audit=audit
    )


def finish_run(device: RestrictedDevice | None, receipt_dir: Path | None = None) -> None:
    """Restore tracked settings, write ``command_receipt.json`` and lift the policy."""
    if device is None:
        return
    try:
        device.restore_settings()
    finally:
        try:
            if receipt_dir is not None:
                receipt_dir.mkdir(parents=True, exist_ok=True)
                receipt = device.audit.receipt(device.run_id)
                (receipt_dir / "command_receipt.json").write_text(json.dumps(receipt, indent=2))
        finally:
            unrestrict(device.serial)
