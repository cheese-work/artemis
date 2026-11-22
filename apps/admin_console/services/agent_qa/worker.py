"""Scripted SmartQA on a leased, dedicated local emulator; no model calls."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shlex
import signal
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
import uuid
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.schemas.task_schema import SmartQAOptions, SmartQARouteStep
from apps.admin_console.services.agent_qa.session import Step, ThemeRun
from artemis.runtime import DeviceExecutionLock, trace_store
from artemis.runtime.cancel_requests import is_cancel_requested
from artemis.runtime.device_lock import DeviceBusyError
from artemis.runtime.endpoint_transport import EndpointTransport


PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
SERIAL = re.compile(r"emulator-[0-9]+")
_APK_PATH = re.compile(r"/data/app/[A-Za-z0-9_./+=~-]+/base\.apk")
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_NIGHT = ("shell", "cmd", "uimode", "night")
_RESUMED = re.compile(r"(?:topResumedActivity|mResumedActivity)\s*[:=].*?\bu\d+ ([\w.]+)/")


class SmartQAError(RuntimeError):
    """A safe diagnostic that never contains device output or route input."""


class _Device:
    def __init__(
        self,
        transport: EndpointTransport,
        serial: str,
        package: str,
        cancelled: Callable[[], bool] | None,
    ):
        self.transport = transport
        self.serial = serial
        self.package = package
        self.cancelled = cancelled

    def run(self, *args: str, restoring: bool = False) -> bytes:
        if not restoring and self.cancelled and self.cancelled():
            raise KeyboardInterrupt
        try:
            result = self.transport.run(["-s", self.serial, *args], capture_output=True, timeout=60)
        except (OSError, TimeoutExpired):
            raise SmartQAError("smartqa_device_command_failed") from None
        if result.returncode:
            raise SmartQAError("smartqa_device_command_failed")
        return result.stdout

    def text(self, *args: str, restoring: bool = False) -> str:
        return self.run(*args, restoring=restoring).decode(errors="replace").strip()

    def installed_digest(self) -> str:
        lines = self.text("shell", "pm", "path", self.package).splitlines()
        if len(lines) != 1 or not lines[0].startswith("package:"):
            raise SmartQAError("smartqa_installed_apk_unverified")
        path = lines[0].removeprefix("package:")
        if not _APK_PATH.fullmatch(path) or ".." in Path(path).parts:
            raise SmartQAError("smartqa_installed_apk_unverified")
        output = self.text("shell", "sha256sum", path).split()
        if len(output) != 2 or output[1] != path or not re.fullmatch(r"[0-9a-f]{64}", output[0]):
            raise SmartQAError("smartqa_installed_apk_unverified")
        return output[0]

    def require_foreground(self) -> None:
        match = _RESUMED.search(self.text("shell", "dumpsys", "activity", "activities"))
        if not match or match[1] != self.package:
            raise SmartQAError("smartqa_left_test_app")

    def capture(self) -> tuple[Image.Image, list[dict]]:
        self.require_foreground()
        remote = f"/data/local/tmp/artemis-smartqa-{uuid.uuid4().hex}.xml"
        try:
            self.run("shell", "uiautomator", "dump", remote)
            hierarchy = self.run("exec-out", "cat", remote)
        finally:
            self.run("shell", "rm", "-f", remote, restoring=True)
        raw = self.run("exec-out", "screencap", "-p")
        self.require_foreground()
        with Image.open(BytesIO(raw)) as image:
            frame = image.convert("RGB")
        nodes = []
        for node in ET.fromstring(hierarchy).iter("node"):
            match = _BOUNDS.fullmatch(node.get("bounds", ""))
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if not (left < right <= frame.width and top < bottom <= frame.height):
                continue
            nodes.append(
                {
                    "text": node.get("text") or node.get("content-desc", ""),
                    "id": node.get("resource-id", ""),
                    "password": node.get("password") == "true",
                    "foreign": node.get("package") != self.package,
                    "bounds": (left, top, right, bottom),
                }
            )
        if not any(not node["foreign"] for node in nodes):
            raise SmartQAError("smartqa_hierarchy_unavailable")
        return frame, nodes

    def act(self, step: SmartQARouteStep, frame: Image.Image, nodes: list[dict]) -> None:
        self.require_foreground()
        if step.action in {"tap", "type"}:
            targets = [
                node
                for node in nodes
                if not node["foreign"]
                and (not step.id or step.id in (node["id"], node["id"].split(":id/")[-1]))
                and (not step.text or (not node["password"] and step.text == node["text"]))
            ]
            if len(targets) != 1 or (step.action == "type" and targets[0]["password"]):
                raise SmartQAError("smartqa_route_selector_unusable")
            left, top, right, bottom = targets[0]["bounds"]
            self.run("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
            if step.action == "type":
                self.require_foreground()
                self.run("shell", "input", "text", shlex.quote(step.value.replace(" ", "%s")))
        elif step.action == "back":
            self.run("shell", "input", "keyevent", "KEYCODE_BACK")
        else:
            short_x, long_x = frame.width // 4, 3 * frame.width // 4
            short_y, long_y = frame.height // 4, 3 * frame.height // 4
            mid_x, mid_y = frame.width // 2, frame.height // 2
            coordinates = {
                "up": (mid_x, long_y, mid_x, short_y),
                "down": (mid_x, short_y, mid_x, long_y),
                "left": (long_x, mid_y, short_x, mid_y),
                "right": (short_x, mid_y, long_x, mid_y),
            }
            self.run("shell", "input", "swipe", *map(str, coordinates[step.direction]), "300")


def _save_capture(
    frame: Image.Image, nodes: list[dict], path: Path, typed: set[str]
) -> tuple[dict, ...]:
    draw = ImageDraw.Draw(frame)
    evidence = []
    for node in nodes:
        left, top, right, bottom = node["bounds"]
        if node["foreign"] or node["password"] or any(value in node["text"] for value in typed):
            draw.rectangle((left, top, right - 1, bottom - 1), fill="black")
        elif node["text"]:
            evidence.append({"text": node["text"], "bounds": node["bounds"], "kind": "text"})
    frame.save(path, format="PNG")
    return tuple(evidence)


def _screen(nodes: list[dict], declared: list[str]) -> str:
    matches = {node["id"].split(":id/")[-1] for node in nodes if not node["foreign"]}
    names = matches.intersection(declared)
    return next(iter(names)) if len(names) == 1 else ""


def _logs(device: _Device, since: str) -> tuple[str, str]:
    package = re.escape(device.package)
    crash = device.text("shell", "logcat", "-b", "crash", "-d", "-v", "raw", "-T", since)
    anrs = device.text(
        "shell", "logcat", "-b", "events", "-d", "-v", "raw", "-T", since, "am_anr:I", "'*:S'"
    )
    blocks = re.split(r"FATAL EXCEPTION", crash)[1:]
    java = sum(
        bool(re.search(rf"^Process:\s*{package}(?=[,:\s]|$)", block, re.M)) for block in blocks
    )
    native = len(re.findall(rf">>>\s*{package}(?::[^\s<]+)?\s*<<<", crash))
    count = len(re.findall(rf"^\[\d+,\s*\d+,\s*{package}(?::[^,]+)?,", anrs, re.M))
    return (
        (f"FATAL EXCEPTION\nProcess: {device.package}\n" * java)
        + (f">>> {device.package} <<<\n" * native),
        f"ANR in {device.package}\n" * count,
    )


def record_route(
    options: SmartQAOptions,
    package: str,
    apk: Path,
    serial: str,
    transport: EndpointTransport,
    trace: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    if not PACKAGE.fullmatch(package) or not SERIAL.fullmatch(serial) or options.mode != "route":
        raise SmartQAError("smartqa_route_binding_invalid")
    if not transport.endpoint.is_local_default:
        raise SmartQAError("smartqa_device_not_dedicated")
    with apk.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != options.apk_sha256:
            raise SmartQAError("smartqa_uploaded_apk_mismatch")
    device = _Device(transport, serial, package, cancelled)
    if device.text("install", "-r", str(apk)) != "Success":
        raise SmartQAError("smartqa_install_failed")
    installed = device.installed_digest()
    if installed != options.apk_sha256:
        raise SmartQAError("smartqa_installed_apk_mismatch")
    if device.text("shell", "pm", "clear", package) != "Success":
        raise SmartQAError("smartqa_clear_data_failed")
    previous = device.text(*_NIGHT).removeprefix("Night mode: ")
    if previous not in {"auto", "yes", "no"}:
        raise SmartQAError("smartqa_night_mode_unrestorable")
    since = device.text("shell", "date", "+%s.%N")
    if not re.fullmatch(r"\d{10}\.\d{1,9}", since):
        raise SmartQAError("smartqa_log_clock_unavailable")
    target_mode = "yes" if options.theme == "dark" else "no"
    trace.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".smartqa-", dir=trace) as stage:
        directory = Path(stage) / "evidence"
        directory.mkdir()
        steps = []
        route_error = None
        try:
            device.run(*_NIGHT, target_mode)
            if device.text(*_NIGHT) != f"Night mode: {target_mode}":
                raise SmartQAError("smartqa_theme_not_applied")
            activity = device.text(
                "shell",
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-a",
                "android.intent.action.MAIN",
                "-c",
                "android.intent.category.LAUNCHER",
                package,
            ).splitlines()
            component = activity[-1] if activity else ""
            if not re.fullmatch(re.escape(package) + r"/[A-Za-z0-9_.$]+", component):
                raise SmartQAError("smartqa_launcher_unavailable")
            device.run("shell", "am", "start", "-W", "-n", shlex.quote(component))
            typed = {step.value for step in options.route if step.action == "type"}
            for number, action in enumerate(options.route, start=1):
                before_png, after_png = f"{number}-before.png", f"{number}-after.png"
                before, before_nodes = device.capture()
                device.act(action, before, before_nodes)
                after, after_nodes = device.capture()
                _save_capture(before, before_nodes, directory / before_png, typed)
                text = _save_capture(after, after_nodes, directory / after_png, typed)
                steps.append(
                    Step(
                        number,
                        options.theme,
                        datetime.now(UTC).isoformat(),
                        _screen(before_nodes, options.screens),
                        _screen(after_nodes, options.screens),
                        {"type": action.action},
                        before_png,
                        after_png,
                        "route",
                        text,
                    )
                )
        except (SmartQAError, ET.ParseError, OSError) as exc:
            route_error = exc
        finally:
            device.run(*_NIGHT, previous, restoring=True)
            if device.text(*_NIGHT, restoring=True) != f"Night mode: {previous}":
                raise SmartQAError("smartqa_theme_not_restored")
        crash, log = _logs(device, since)
        if device.installed_digest() != installed:
            raise SmartQAError("smartqa_installed_apk_mismatch")
        if route_error and not (crash or log):
            raise SmartQAError("smartqa_route_failed") from None
        session = {
            "package": package,
            "declared_screens": [{"id": name} for name in options.screens],
            "themes": {options.theme: asdict(ThemeRun(tuple(steps), crash, log, installed))},
        }
        (directory / "session.json").write_text(json.dumps(session), encoding="utf-8")
        directory.rename(trace / "smartqa")
        if route_error:
            raise SmartQAError("smartqa_route_failed") from None


def main() -> int:
    from apps.admin_console.services.agent_qa.binding import verify_device, stored_apk

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        envelope = json.loads(os.environ.pop("ARTEMIS_SMARTQA_WORKER"))
        if not isinstance(envelope, dict) or not isinstance(envelope.get("package"), str):
            raise ValueError("Invalid SmartQA worker envelope")
        options = SmartQAOptions.model_validate(envelope["options"])
        serial = os.environ["ADB_DEVICE_SERIAL"]
        session_id = os.environ["ARTEMIS_SESSION_ID"]
        transport = EndpointTransport.current()
        lock = DeviceExecutionLock(serial, description="SmartQA route", session_id=session_id)
        try:
            lock.acquire(timeout=60)
            verify_device(options, envelope["holder"], transport, serial)
            record_route(
                options,
                envelope["package"],
                stored_apk(options),
                serial,
                transport,
                Path(trace_store.get_trace_dir(session_id)),
                cancelled=lambda: is_cancel_requested(session_id=session_id),
            )
        finally:
            lock.release()
    except KeyboardInterrupt:
        return 130
    except SmartQAError as exc:
        print(str(exc), flush=True)
        return 1
    except AdminAPIError as exc:
        print(exc.code, flush=True)
        return 1
    except (KeyError, ValueError, OSError, DeviceBusyError):
        print("smartqa_worker_failed", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
