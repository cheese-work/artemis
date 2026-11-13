"""Host endpoints: resolve at launch, offline hosts, local-only operations, recording (CHE-1094)."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.config import settings
from artemis.runtime.adb_endpoint import AdbEndpoint, AdbTarget
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.host_endpoints import HostEndpointRegistry, HostOffline, host_endpoints


@pytest.fixture(autouse=True)
def host_agent(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    yield
    for host_id in ("lab-1", "lab-2"):
        host_endpoints.unregister(host_id)


def _endpoint(port: int, generation: int, host_id: str = "lab-1") -> AdbEndpoint:
    return AdbEndpoint.create("127.0.0.1", port, host_id=host_id, generation=generation)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_resolves_the_live_tunnel_and_fences_stale_generations():
    registry = HostEndpointRegistry()
    first, second = _endpoint(40000, 1), _endpoint(40001, 2)

    registry.register(first)
    registry.register(second)
    registry.register(first)  # a late, older connection must not win

    assert registry.resolve("lab-1") == second
    assert registry.is_current(second) and not registry.is_current(first)


def test_unregister_only_removes_the_named_generation():
    registry = HostEndpointRegistry()
    registry.register(_endpoint(40001, 2))

    registry.unregister("lab-1", generation=1)
    assert registry.resolve("lab-1").generation == 2
    registry.unregister("lab-1", generation=2)

    with pytest.raises(HostOffline, match="lab-1"):
        registry.resolve("lab-1")


def test_only_host_endpoints_can_be_registered():
    with pytest.raises(ValueError):
        HostEndpointRegistry().register(AdbEndpoint.local())


# --------------------------------------------------------------------------- #
# Queued work resolves its endpoint at launch
# --------------------------------------------------------------------------- #


def _queued_item(endpoint: AdbEndpoint) -> dict:
    return {
        "session_id": "s-1",
        "goal": "open settings",
        "device_serial": "emulator-5554",
        "adb_endpoint": endpoint.to_dict(),
        "ingress": "frontend",
    }


def test_the_scheduler_keeps_the_snapshot_so_a_port_change_cannot_split_the_lock():
    item = _queued_item(_endpoint(40000, 1))
    host_endpoints.register(_endpoint(40777, 5))

    snapshot = TaskQueueService._task_target(item)

    assert snapshot.endpoint.port == 40000
    assert snapshot.lock_key == "host:lab-1/emulator-5554"


def test_launch_swaps_in_the_hosts_live_endpoint():
    item = _queued_item(_endpoint(40000, 1))
    host_endpoints.register(_endpoint(40777, 5))

    target = TaskQueueService._task_target(item, resolve_host=True)
    _command, env = TaskQueueService._build_worker_invocation(
        item, "run-key-0001", "s-1", "open settings", "default", target, {}
    )

    assert target.endpoint.port == 40777 and target.endpoint.generation == 5
    assert env["ADB_PORT"] == "40777" and env["ANDROID_ADB_SERVER_PORT"] == "40777"
    assert env["ARTEMIS_ADB_HOST_ID"] == "lab-1" and env["ARTEMIS_ADB_GENERATION"] == "5"
    assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == "host:lab-1"


def test_launch_for_an_offline_host_raises_before_any_worker_is_built():
    item = _queued_item(_endpoint(40000, 1))

    with pytest.raises(HostOffline):
        TaskQueueService._task_target(item, resolve_host=True)


@pytest.mark.asyncio
async def test_an_offline_host_fails_the_task_instead_of_spawning_a_worker(monkeypatch):
    spawned = []

    async def no_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a worker must not start for an offline host")

    persisted = []

    async def record_failure(sess_id, returncode, manual_stop):
        persisted.append((sess_id, returncode))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    monkeypatch.setattr(TaskQueueService, "_begin_task_run", classmethod(lambda cls, *a: None))
    monkeypatch.setattr(
        TaskQueueService,
        "_persist_terminal_session_status",
        classmethod(lambda cls, *a, **k: record_failure(*a, **k)),
    )
    monkeypatch.setattr(TaskQueueService, "_deliver_outcome", classmethod(lambda cls, *a: None))

    await TaskQueueService._execute_task_item(_queued_item(_endpoint(40000, 1)))

    assert spawned == [] and persisted == [("s-1", 1)]


# --------------------------------------------------------------------------- #
# Local-only operations and recording never run for a host
# --------------------------------------------------------------------------- #


@pytest.fixture
def as_host_process(monkeypatch):
    endpoint = _endpoint(40000, 1)
    environment: dict[str, str] = {}
    endpoint.apply_to_environment(environment)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "ADB_HOST", endpoint.host)
    monkeypatch.setattr(settings, "ADB_PORT", endpoint.port)
    return endpoint


@pytest.fixture
def spawn_trap(monkeypatch):
    spawned: list = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: spawned.append(argv))
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, *a, **k: spawned.append(argv) or pytest.fail("spawn")
    )
    return spawned


@pytest.mark.asyncio
async def test_restarting_the_adb_server_is_skipped_for_a_host_process(as_host_process, spawn_trap):
    from artemis.core.diagnostics.engine import ReadinessEngine

    result = await ReadinessEngine().restart_adb_server()

    assert result["skipped"] is True and spawn_trap == []


def test_key_healing_never_restarts_a_server_for_a_host_process(
    as_host_process, spawn_trap, monkeypatch
):
    from artemis.core.diagnostics import adb_keys

    monkeypatch.setattr(
        adb_keys,
        "inspect_adb_keys",
        lambda: adb_keys.AdbKeyStatus(
            is_valid=False,
            is_corrupted=True,
            exists=True,
            key_path="/nonexistent/adbkey",
            pub_key_path="/nonexistent/adbkey.pub",
            error_reason="empty",
        ),
    )

    result = adb_keys.heal_adb_keys(force=True)

    assert result["success"] is False and spawn_trap == []


@pytest.mark.asyncio
async def test_device_pool_warm_up_never_starts_a_server_for_a_host(as_host_process, spawn_trap):
    from artemis.runtime.device_pool import DevicePool

    pool = DevicePool(adb_path="adb-sentinel")

    await pool._start_adb_server(timeout=1)

    assert spawn_trap == []


@pytest.mark.asyncio
async def test_recording_is_skipped_for_host_runs(as_host_process, tmp_path):
    from artemis.controllers.unified_controller import UnifiedMobileController
    from artemis.utils.video import HOST_RECORDING_UNAVAILABLE

    controller = UnifiedMobileController.__new__(UnifiedMobileController)
    controller._segment_cache = {}
    controller._get_device_id = lambda: "emulator-5554"

    result = await controller.start_video_recording(output_dir=tmp_path)

    assert result.success is False and result.message == HOST_RECORDING_UNAVAILABLE


@pytest.mark.asyncio
async def test_recording_watchdog_does_nothing_for_host_runs(as_host_process):
    from artemis.controllers.unified_controller import UnifiedMobileController

    controller = UnifiedMobileController.__new__(UnifiedMobileController)

    # Returns immediately instead of polling display state twice a second.
    await asyncio.wait_for(controller._recording_watchdog("emulator-5554"), timeout=1)


def test_a_host_target_is_never_a_persistable_preference():
    target = AdbTarget(_endpoint(40000, 1), "emulator-5554")

    assert target.endpoint.persistable is False
