"""Regression pins for today's local adb path (CHE-1094, written before the refactor).

These describe what every adb call must keep doing for a normal local run, so
the endpoint-aware transport refactor can prove it changed nothing there:

* the process endpoint resolves to the local server by default and an explicit
  preference is honoured;
* each subprocess-based adb call carries that endpoint as ``-H/-P``;
* a worker is spawned with the run's endpoint in its environment and the lock
  scope derived from it;
* the local-only maintenance calls (warm-up ``start-server``) still fire for
  the local server and never for a remote one.

They assert behaviour (the endpoint a call reaches), not which helper builds
the command line, so they hold on both sides of the refactor.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from artemis.config import settings
from artemis.runtime.adb_endpoint import (
    AdbEndpoint,
    AdbTarget,
    current_adb_endpoint,
)
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.device_pool import DevicePool

LOCAL = ("127.0.0.1", "5037")
REMOTE = ("192.0.2.7", "5555")


@pytest.fixture
def adb_calls(monkeypatch):
    """Capture every ``subprocess.run`` argv + env without running anything."""
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, *_args, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or {})))
        return subprocess.CompletedProcess(argv, 0, stdout="device\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "artemis.runtime.adb_endpoint.toolchain.resolve",
        lambda name: "adb-sentinel" if name == "adb" else None,
    )
    return calls


def _use_endpoint(monkeypatch, host: str, port: str) -> None:
    monkeypatch.setattr(settings, "ADB_HOST", host)
    monkeypatch.setattr(settings, "ADB_PORT", int(port))
    monkeypatch.delenv("ADB_HOST", raising=False)
    monkeypatch.delenv("ADB_PORT", raising=False)


def _server_flags(argv: list[str]) -> tuple[str, str]:
    """The adb server a command line addresses, as (host, port)."""
    assert "-H" in argv and "-P" in argv, f"adb call without an explicit server: {argv}"
    return argv[argv.index("-H") + 1], argv[argv.index("-P") + 1]


# --------------------------------------------------------------------------- #
# Endpoint resolution + identity
# --------------------------------------------------------------------------- #


def test_default_process_endpoint_is_the_local_server(monkeypatch):
    _use_endpoint(monkeypatch, *LOCAL)

    endpoint = current_adb_endpoint()

    assert (endpoint.host, str(endpoint.port)) == LOCAL
    assert endpoint.mode == "local" and endpoint.is_local_default


def test_explicit_preference_is_a_remote_endpoint(monkeypatch):
    _use_endpoint(monkeypatch, *REMOTE)

    endpoint = current_adb_endpoint()

    assert (endpoint.host, str(endpoint.port)) == REMOTE
    assert endpoint.mode == "remote" and not endpoint.is_local_default


def test_target_lock_key_is_endpoint_scoped_serial():
    target = AdbTarget(endpoint=AdbEndpoint.create("192.0.2.7", 5555), serial="emulator-5554")

    assert target.lock_scope == "tcp:192.0.2.7:5555"
    assert target.lock_key == "tcp:192.0.2.7:5555/emulator-5554"
    assert AdbTarget(endpoint=AdbEndpoint.local()).lock_key == "tcp:127.0.0.1:5037/pending"


def test_apply_to_environment_keeps_the_existing_adb_variables():
    environment: dict[str, str] = {}

    AdbEndpoint.create("192.0.2.7", 5555).apply_to_environment(environment)

    assert environment["ADB_HOST"] == "192.0.2.7"
    assert environment["ADB_PORT"] == "5555"
    assert environment["ADB_SERVER_SOCKET"] == "tcp:192.0.2.7:5555"
    assert environment["ARTEMIS_ADB_ENDPOINT_ID"] == "tcp:192.0.2.7:5555"


def test_worker_environment_carries_the_runs_endpoint_and_lock_scope():
    from apps.admin_console.services.task_queue_service import TaskQueueService

    endpoint = AdbEndpoint.create("192.0.2.7", 5555)
    target = AdbTarget(endpoint=endpoint, serial="emulator-5554")

    _command, env = TaskQueueService._build_worker_invocation(
        {"ingress": "frontend"}, "run-key-0001", "sess-1", "open settings", "default", target, {}
    )

    assert env["ADB_HOST"] == "192.0.2.7" and env["ADB_PORT"] == "5555"
    assert env["ADB_SERVER_SOCKET"] == "tcp:192.0.2.7:5555"
    assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == "tcp:192.0.2.7:5555"


# --------------------------------------------------------------------------- #
# Subprocess adb calls address the process endpoint
# --------------------------------------------------------------------------- #


def _call_ui_automator_package_check() -> None:
    from artemis.clients import ui_automator_client

    ui_automator_client._is_package_installed("emulator-5554", "com.example")


def _call_screen_factory_state() -> None:
    from artemis.clients import screen_client_factory

    screen_client_factory.device_state("emulator-5554")


def _call_awake_command() -> None:
    from artemis.runtime import awake_service

    awake_service._run_awake_adb_command("emulator-5554", ["shell", "true"], "probe")


def _call_helper_default_runner() -> None:
    from artemis.runtime.helper_manager import AccessibilityHelperManager

    AccessibilityHelperManager()._run_adb(["-s", "emulator-5554", "get-state"])


def _call_accessibility_press_key() -> None:
    from artemis.clients.accessibility_client import AccessibilityClient

    client = AccessibilityClient("emulator-5554", manager=SimpleNamespace(), request_timeout=1.0)
    client.press_key("enter")


@pytest.mark.parametrize(
    "call",
    [
        _call_ui_automator_package_check,
        _call_screen_factory_state,
        _call_awake_command,
        _call_helper_default_runner,
        _call_accessibility_press_key,
    ],
)
@pytest.mark.parametrize("server", [LOCAL, REMOTE], ids=["local", "remote"])
def test_subprocess_adb_calls_address_the_process_endpoint(monkeypatch, adb_calls, call, server):
    _use_endpoint(monkeypatch, *server)

    call()

    assert adb_calls, "the call did not reach adb"
    for argv, _env in adb_calls:
        assert _server_flags(argv) == server


# --------------------------------------------------------------------------- #
# Device pool: enumeration and the local-only warm-up
# --------------------------------------------------------------------------- #


def _recording_async_exec(monkeypatch, calls: list[list[str]]) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"List of devices attached\nemulator-5554\tdevice model:Fake\n", b""

        def kill(self):
            return None

    async def fake_exec(*argv, **_kwargs):
        calls.append(list(argv))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_pool_enumerates_with_adb_devices_long(monkeypatch):
    _use_endpoint(monkeypatch, *LOCAL)
    calls: list[list[str]] = []
    _recording_async_exec(monkeypatch, calls)
    pool = DevicePool(adb_path="adb-sentinel")

    devices = asyncio.run(pool.list_devices_async())

    assert [(d.serial, d.state) for d in devices] == [("emulator-5554", "device")]
    assert calls and calls[0][-2:] == ["devices", "-l"]


def test_warm_up_starts_the_local_server_first(monkeypatch):
    _use_endpoint(monkeypatch, *LOCAL)
    calls: list[list[str]] = []
    _recording_async_exec(monkeypatch, calls)
    pool = DevicePool(adb_path="adb-sentinel")

    warmed = asyncio.run(pool.warm_up_async(settle_timeout=0))

    assert warmed is True
    assert calls[0][-1] == "start-server"
    assert calls[1][-2:] == ["devices", "-l"]
