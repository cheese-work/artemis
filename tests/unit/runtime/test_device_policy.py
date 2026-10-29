"""Restricted device command policy, audit log and ``left_test_app`` stop (CHE-2129, S3.2b).

Device-free. Every sink that could reach a device (``subprocess.run``, ``Popen``,
``asyncio.create_subprocess_exec``) is a spy, so "refused" is proven as *zero calls
to the device transport*. One test sends allowed and refused commands through the
real ``adb`` binary to a fake adb server.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from artemis.runtime import device_policy
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_policy import (
    AuditLog,
    DevicePolicyDenied,
    LeftTestApp,
    as_caller,
    restrict,
    unrestrict,
)
from artemis.runtime.endpoint_transport import EndpointTransport

SERIAL = "XIAOMI-GUEST"
PKG = "com.cheese.smartqa.fixture"
ESCAPE = "com.cheese.smartqa.fixture.escape"
UID = 10234
APK = "/work/smartqa-fixture.apk"
ENDPOINT = AdbEndpoint.create("127.0.0.1", 5037)


def _resumed(package: str) -> bytes:
    return (
        f"  ResumedActivity: ActivityRecord{{a1b2 u0 {package}/.MainActivity t12}}\n"
        "  mFocusedApp=...\n"
    ).encode()


class Sink:
    """Records every call that would have reached a device; answers from ``replies``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.foreground = PKG
        self.png = b"\x89PNG-fixture"
        self.replies: dict[str, bytes] = {}

    def reply(self, arguments) -> bytes:
        text = " ".join(arguments)
        if "dumpsys activity activities" in text:
            return _resumed(self.foreground)
        if "screencap" in text:
            return self.png
        for needle, out in self.replies.items():
            if needle in text:
                return out
        return b"ok\n"

    def run(self, command, **_kw):
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=self.reply(command), stderr=b"")


@pytest.fixture
def sink(monkeypatch):
    spy = Sink()
    monkeypatch.setattr("artemis.runtime.adb_endpoint.subprocess.run", spy.run)

    def popen(command, **_kw):
        spy.calls.append(list(command))
        return SimpleNamespace()

    async def create(*command, **_kw):
        spy.calls.append(list(command))
        return SimpleNamespace()

    monkeypatch.setattr("artemis.runtime.endpoint_transport.subprocess.Popen", popen)
    monkeypatch.setattr("artemis.runtime.endpoint_transport.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("artemis.runtime.adb_endpoint.toolchain.resolve", lambda _n: "adb")
    return spy


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    device_policy.reset()


@pytest.fixture
def transport():
    return EndpointTransport(ENDPOINT, adb_path="adb")


@pytest.fixture
def audit(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def device(monkeypatch, transport, audit):
    monkeypatch.setenv("ARTEMIS_DEVICE_POLICY", "on")
    return restrict(
        transport,
        SERIAL,
        test_packages=frozenset({PKG, ESCAPE}),
        test_uid=UID,
        locked_package=PKG,
        apk_paths=frozenset({APK}),
        run_id="run-1",
        audit=audit,
    )


def decisions(audit: AuditLog) -> list[str]:
    return [e["decision"] for e in audit.entries()]


# --------------------------------------------------------------------------- flag


def test_flag_off_by_default_restrict_is_a_no_op(monkeypatch, transport, audit, sink):
    monkeypatch.delenv("ARTEMIS_DEVICE_POLICY", raising=False)
    assert (
        restrict(transport, SERIAL, test_packages=frozenset({PKG}), test_uid=UID, audit=audit)
        is None
    )

    transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"])

    assert sink.calls == [
        ["adb", "-H", "127.0.0.1", "-P", "5037", "-s", SERIAL, "shell", "pm", "list", "packages"]
    ]
    assert audit.entries() == []


def test_other_serials_are_untouched_when_one_is_restricted(device, transport, sink):
    transport.run(["-s", "emulator-5554", "shell", "pm", "list", "packages"])
    assert len(sink.calls) == 1


# ------------------------------------------------------------------------ allowed


ALLOWED = [
    ["install", "-r", APK],
    ["uninstall", PKG],
    ["uninstall", ESCAPE],
    ["shell", "am", "start", "-n", f"{PKG}/.MainActivity"],
    ["shell", f"am start -W -n {PKG}/.MainActivity"],
    ["shell", "dumpsys", "activity", "activities"],
    ["shell", "logcat", "-d", "--uid", str(UID)],
    ["shell", "logcat", "-b", "crash", "-d", "--uid", str(UID)],
    ["shell", "cmd", "uimode", "night"],
    ["shell", "cmd", "uimode", "night", "yes"],
    ["shell", "settings", "put", "global", "window_animation_scale", "0"],
    ["shell", "settings", "get", "global", "animator_duration_scale"],
    ["shell", "settings", "put", "global", "stay_on_while_plugged_in", "3"],
]


@pytest.mark.parametrize("command", ALLOWED, ids=[" ".join(c) for c in ALLOWED])
def test_allowlisted_command_passes_and_is_audited_allowed(device, transport, sink, audit, command):
    transport.run(["-s", SERIAL, *command])

    assert len(sink.calls) == 1
    (entry,) = audit.entries()
    assert entry["decision"] == "allowed"
    assert entry["serial"] == SERIAL
    assert entry["caller"] == "smartqa"
    assert entry["run_id"] == "run-1"
    assert entry["time"]


def test_caller_is_recorded(device, transport, sink, audit):
    with as_caller("llm_agent"):
        transport.run(["-s", SERIAL, "shell", "cmd", "uimode", "night"])
    assert audit.entries()[0]["caller"] == "llm_agent"


def test_install_logs_the_apk_name_not_its_directory(device, transport, sink, audit):
    transport.run(["-s", SERIAL, "install", APK])
    assert audit.entries()[0]["command"] == "install smartqa-fixture.apk"


# ------------------------------------------------------------------------- denied

DENIED = [
    ["shell", "pm", "list", "packages"],
    ["shell", "content", "query", "--uri", "content://sms/inbox"],
    ["shell", "pm", "clear", "com.other.bank"],
    ["shell", "run-as", "com.other.bank", "ls"],
    ["bugreport"],
    # shell metacharacters smuggled behind an allowed prefix
    ["shell", "cmd uimode night yes; pm list packages"],
    ["shell", "cmd uimode night yes\npm list packages"],
    ["shell", "cmd uimode night yes && content query --uri content://sms"],
    ["shell", "am", "start", "-n", f"{PKG}/.Main$(pm list packages)"],
    # near misses of allowed forms
    ["uninstall", "com.other.bank"],
    ["install", "/other/evil.apk"],
    ["shell", "am", "start", "-n", "com.other.bank/.Main"],
    ["shell", "logcat", "-d"],
    ["shell", "logcat", "-d", "--uid", "1000"],
    ["shell", "settings", "put", "secure", "location_mode", "0"],
    ["shell", "settings", "put", "global", "device_name", "x"],
    ["shell", "input", "keyevent", "KEYCODE_HOME"],
    ["pull", "/sdcard/DCIM/Camera/photo.jpg", "."],
    ["shell", "screencap", "-p"],  # no foreground check first
    ["exec-out", "screencap", "-p"],
    ["shell"],
    [],
]


@pytest.mark.parametrize("command", DENIED, ids=[" ".join(c)[:60] or "empty" for c in DENIED])
def test_other_commands_are_refused_before_the_transport(device, transport, sink, audit, command):
    with pytest.raises(DevicePolicyDenied) as raised:
        transport.run(["-s", SERIAL, *command])

    assert raised.value.code == "device_policy_denied"
    assert sink.calls == []
    assert decisions(audit) == ["denied"]


def test_refused_commands_never_reach_the_async_or_popen_paths(device, transport, sink, audit):
    with pytest.raises(DevicePolicyDenied):
        transport.popen(["-s", SERIAL, "shell", "content", "query"])
    with pytest.raises(DevicePolicyDenied):
        asyncio.run(transport.create_subprocess(["-s", SERIAL, "bugreport"]))
    assert sink.calls == []
    assert decisions(audit) == ["denied", "denied"]


def test_allowed_commands_reach_the_async_and_popen_paths(device, transport, sink, audit):
    transport.popen(["-s", SERIAL, "shell", "logcat", "-d", "--uid", str(UID)])
    asyncio.run(transport.create_subprocess(["-s", SERIAL, "shell", "cmd", "uimode", "night"]))
    assert len(sink.calls) == 2
    assert decisions(audit) == ["allowed", "allowed"]


def test_command_without_a_serial_is_refused_while_a_device_is_restricted(
    device, transport, sink, audit
):
    with pytest.raises(DevicePolicyDenied):
        transport.run(["shell", "pm", "list", "packages"])
    transport.run(["devices"])  # host-level, names no device
    assert len(sink.calls) == 1


def test_denied_entries_carry_no_arguments(device, transport, sink, audit):
    for command in DENIED[:4]:
        with pytest.raises(DevicePolicyDenied):
            transport.run(["-s", SERIAL, *command])

    text = (audit.path).read_text()
    for private in ("com.other.bank", "content://sms", "inbox", "packages"):
        assert private not in text
    assert [e["command"] for e in audit.entries()][:2] == ["shell pm", "shell content"]


def test_adbutils_and_u2_access_to_a_restricted_serial_is_refused(device, transport, audit):
    with pytest.raises(DevicePolicyDenied):
        transport.device(SERIAL)
    with pytest.raises(DevicePolicyDenied):
        transport.u2_connect(SERIAL)
    assert decisions(audit) == ["denied", "denied"]


def test_unrestrict_lifts_the_policy(device, transport, sink):
    unrestrict(SERIAL)
    transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"])
    assert len(sink.calls) == 1


# ---------------------------------------------------------------------- receipt


def test_receipt_exports_one_runs_entries(device, transport, sink, audit):
    transport.run(["-s", SERIAL, "shell", "cmd", "uimode", "night"])
    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, "bugreport"])
    restrict(
        transport,
        "OTHER",
        test_packages=frozenset({PKG}),
        test_uid=UID,
        run_id="run-2",
        audit=audit,
    )
    transport.run(["-s", "OTHER", "shell", "cmd", "uimode", "night"])

    receipt = audit.receipt("run-1")

    assert receipt["run_id"] == "run-1"
    assert [(e["decision"], e["command"]) for e in receipt["entries"]] == [
        ("allowed", "shell cmd uimode night"),
        ("denied", "bugreport"),
    ]
    json.dumps(receipt)  # serialisable as the run's command receipt


# ------------------------------------------------------------ screenshot / left app


def test_screenshot_after_foreground_check_is_written(device, sink, tmp_path, audit):
    dest = tmp_path / "step1.png"

    device.capture_screenshot(dest)

    assert dest.read_bytes() == sink.png
    assert [e["command"] for e in audit.entries()] == [
        "shell dumpsys activity activities",
        "exec-out screencap -p",
        "shell dumpsys activity activities",
    ]
    assert set(decisions(audit)) == {"allowed"}


def test_screencap_needs_a_fresh_foreground_check_each_time(device, transport, sink, audit):
    device.check_foreground()
    transport.run(["-s", SERIAL, "exec-out", "screencap", "-p"])
    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, "exec-out", "screencap", "-p"])


def test_foreground_change_stops_the_run_and_persists_no_png(device, sink, tmp_path, audit):
    sink.foreground = ESCAPE.replace(".escape", ".elsewhere")  # not a test package
    dest = tmp_path / "step2.png"

    with pytest.raises(LeftTestApp) as raised:
        device.capture_screenshot(dest)

    assert raised.value.code == "left_test_app"
    assert not dest.exists()
    assert not any("screencap" in " ".join(c) for c in sink.calls)


def test_foreground_change_during_capture_discards_the_png(device, sink, tmp_path, monkeypatch):
    dest = tmp_path / "step3.png"
    original = sink.reply

    def flip_after_capture(arguments):
        out = original(arguments)
        if "screencap" in " ".join(arguments):
            sink.foreground = "com.other.bank"
        return out

    monkeypatch.setattr(sink, "reply", flip_after_capture)

    with pytest.raises(LeftTestApp):
        device.capture_screenshot(dest)

    assert not dest.exists()


def test_unreadable_foreground_counts_as_left_test_app(device, sink, tmp_path):
    sink.reply = lambda _arguments: b"nothing useful\n"
    with pytest.raises(LeftTestApp):
        device.capture_screenshot(tmp_path / "x.png")
    assert not (tmp_path / "x.png").exists()


def test_open_escape_in_fixture_session_stops_with_left_test_app(device, sink, tmp_path, audit):
    """Recorded fixture session: tap "Open escape" moves the foreground to a package that
    is not the run's test app (``locked_app_package``), even though it is a test-owned one."""
    shots = tmp_path / "shots"
    shots.mkdir()

    device.capture_screenshot(shots / "1.png")  # home screen of the fixture
    sink.foreground = ESCAPE  # the tap on "Open escape" lands here
    with pytest.raises(LeftTestApp):
        device.capture_screenshot(shots / "2.png")

    assert [p.name for p in shots.iterdir()] == ["1.png"]
    assert "denied" not in decisions(audit)


# ----------------------------------------------------------- settings record/restore


def test_settings_are_recorded_then_restored(device, sink, audit):
    sink.replies["settings get global window_animation_scale"] = b"1.0\n"
    sink.replies["cmd uimode night"] = b"Night mode: auto\n"

    device.apply_setting("window_animation_scale", "0")
    device.apply_setting("night_mode", "yes")
    device.restore_settings()

    tails = [c[-5:] for c in sink.calls]
    assert ["settings", "put", "global", "window_animation_scale", "0"] in tails
    assert ["settings", "put", "global", "window_animation_scale", "1.0"] in tails
    assert ["cmd", "uimode", "night", "auto"] in [t[-4:] for t in tails]
    assert set(decisions(audit)) == {"allowed"}


# ------------------------------------------------------------- fake adb server


@pytest.mark.skipif(shutil.which("adb") is None, reason="adb binary not installed")
def test_fake_adb_server_sees_allowed_and_never_refused(
    monkeypatch, fake_adb_server_factory, tmp_path
):
    server = fake_adb_server_factory("guest-phone")
    server.add_device(SERIAL)
    transport = EndpointTransport(server.endpoint)
    audit = AuditLog(tmp_path / "audit.jsonl")
    monkeypatch.setenv("ARTEMIS_DEVICE_POLICY", "on")
    restrict(transport, SERIAL, test_packages=frozenset({PKG}), test_uid=UID, audit=audit)

    transport.run(["-s", SERIAL, "shell", "cmd", "uimode", "night"], capture_output=True)
    with pytest.raises(DevicePolicyDenied):
        transport.run(["-s", SERIAL, "shell", "pm", "list", "packages"], capture_output=True)

    seen = [r.request for r in server.requests]
    assert any(r.startswith("shell") and "uimode" in r for r in seen)
    assert not any("pm list" in r for r in seen)
    assert decisions(audit) == ["allowed", "denied"]
