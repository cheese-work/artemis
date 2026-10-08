"""Preview startup/shutdown profile (CHE-1288): no lifecycle side effect may run.

Every named effect is replaced by a hook that fails the test when called; the
normal profile must keep calling the same hooks.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.admin_console import server
from apps.admin_console.core.preview_profile import (
    ENV_PREVIEW_PROFILE,
    preview_profile_selected,
)
from apps.admin_console.core.state import ServerState, state
from apps.admin_console.services import run_retention

REPO_ROOT = Path(__file__).resolve().parents[3]


VIOLATIONS: list[str] = []  # a hook hit inside a background task cannot fail the test directly


def _forbidden(name: str):
    def fail(*_args, **_kwargs):
        VIOLATIONS.append(name)
        raise AssertionError(f"{name} must not run under the preview profile")

    return fail


def _forbidden_async(name: str):
    async def fail(*_args, **_kwargs):
        VIOLATIONS.append(name)
        raise AssertionError(f"{name} must not run under the preview profile")

    return fail


def _logging_snapshot():
    loggers = [logging.getLogger(), *logging.Logger.manager.loggerDict.values()]
    handlers = {
        h: list(h.filters) for lg in loggers if isinstance(lg, logging.Logger) for h in lg.handlers
    }
    return sys.stdout, sys.stderr, handlers


@pytest.fixture(autouse=True)
def _logging_not_leaked():
    """configure_logging() wraps std streams and filters every live handler; no test here may keep that."""
    before = _logging_snapshot()
    yield
    after = _logging_snapshot()
    assert after[:2] == before[:2], "std streams left wrapped"
    assert {h: f for h, f in after[2].items() if h in before[2]} == {
        h: f for h, f in before[2].items() if h in after[2]
    }, "handler filters leaked"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    VIOLATIONS.clear()
    for name, value in vars(ServerState()).items():
        monkeypatch.setattr(state, name, value)


def _stub_hooks(monkeypatch, *, forbidden: bool):
    """Replace every startup/shutdown effect; return the recorder (normal) or None."""
    calls = MagicMock()
    sync_targets = {
        "write_server_info": (server, "write_server_info"),
        "start_awake_service": (server, "start_awake_service"),
        "shutdown_awake_service": (server, "shutdown_awake_service"),
        "clear_server_info": (server, "clear_server_info"),
        "cleanup_stale_locks": (server.DeviceExecutionLock, "cleanup_stale_locks"),
        "cancel_reservation": (server.DeviceExecutionLock, "cancel_reservation"),
        "archive_older_replays_on_launch": (
            server.task_queue_service,
            "archive_older_replays_on_launch",
        ),
        "verify_chunks_exist_on_launch": (
            server.task_queue_service,
            "verify_chunks_exist_on_launch",
        ),
        "recover_orphaned_recordings_on_launch": (
            server.task_queue_service,
            "recover_orphaned_recordings_on_launch",
        ),
        "cleanup_orphans_on_startup": (server.session_repo, "cleanup_orphans_on_startup"),
        "drain_outcome_events": (server.task_queue_service, "_drain_outcome_events"),
        "reset_for_boot": (server.host_registry, "reset_for_boot"),
    }
    async_targets = {
        "warm_up_async": (server.device_pool, "warm_up_async"),
        "ipc_start_server": (server.ipc_service, "start_server"),
        "ipc_stop_server": (server.ipc_service, "stop_server"),
        "queue_worker": (server.task_queue_service, "queue_worker"),
        "sweep_forever": (run_retention, "sweep_forever"),
        "failure_sweep": (server.failure_ledger, "sweep_forever"),
    }
    for name, (owner, attr) in sync_targets.items():
        stub = _forbidden(name) if forbidden else getattr(calls, name)
        if name == "cleanup_orphans_on_startup" and not forbidden:
            stub = lambda *a, **k: 0  # noqa: E731
        monkeypatch.setattr(owner, attr, stub)
    for name, (owner, attr) in async_targets.items():
        stub = _forbidden_async(name) if forbidden else AsyncMock()
        if not forbidden:
            setattr(calls, name, stub)
        monkeypatch.setattr(owner, attr, stub)
    # A host agent that is enabled makes host enrollment reachable on startup.
    monkeypatch.setattr(server, "host_agent_enabled", lambda: True)
    return None if forbidden else calls


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("false", False), ("1", True), ("true", True)],
)
def test_profile_selection_reads_only_the_explicit_flag(value, expected):
    environ = {} if value is None else {ENV_PREVIEW_PROFILE: value}
    assert preview_profile_selected(environ) is expected


@pytest.mark.asyncio
async def test_preview_startup_runs_none_of_the_named_effects(monkeypatch):
    _stub_hooks(monkeypatch, forbidden=True)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", True)
    fixtures = MagicMock()
    monkeypatch.setattr(server, "initialize_preview_fixtures", fixtures)

    await server.on_startup()
    await asyncio.sleep(0.2)  # let any scheduled background task reach its hook

    assert VIOLATIONS == []
    assert state.worker_task is None
    assert state.retention_task is None
    fixtures.assert_called_once_with(server.PREVIEW_ROOT, server.app.state.access_config)
    assert state.is_shutting_down is False


@pytest.mark.asyncio
async def test_preview_shutdown_runs_none_of_the_named_effects(monkeypatch):
    _stub_hooks(monkeypatch, forbidden=True)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", True)
    state.queue_items[:] = [{"queue_ticket": "ticket-1", "status": "queued"}]

    await server.on_shutdown()
    await asyncio.sleep(0.2)

    assert VIOLATIONS == []
    assert state.is_shutting_down is True
    assert state.queue_items == []


@pytest.mark.asyncio
async def test_normal_startup_and_shutdown_still_run_every_effect(monkeypatch):
    calls = _stub_hooks(monkeypatch, forbidden=False)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", False)
    state.queue_items[:] = [{"queue_ticket": "ticket-1", "status": "queued"}]

    await server.on_startup()
    await asyncio.sleep(0.2)
    for task in (state.worker_task, state.retention_task):
        if task is not None:
            task.cancel()
    await asyncio.gather(
        *(t for t in (state.worker_task, state.retention_task) if t), return_exceptions=True
    )
    state.worker_task = None
    state.retention_task = None
    await server.on_shutdown()

    calls.write_server_info.assert_called_once()
    calls.start_awake_service.assert_called_once()
    assert calls.cleanup_stale_locks.call_count == 2  # startup and shutdown
    calls.warm_up_async.assert_awaited_once()
    calls.archive_older_replays_on_launch.assert_called_once()
    calls.verify_chunks_exist_on_launch.assert_called_once()
    calls.recover_orphaned_recordings_on_launch.assert_called_once()
    calls.reset_for_boot.assert_called_once()
    calls.ipc_start_server.assert_awaited_once()
    calls.sweep_forever.assert_called_once()
    calls.failure_sweep.assert_called_once()
    calls.cancel_reservation.assert_called_once_with("ticket-1")
    calls.shutdown_awake_service.assert_called_once()
    calls.clear_server_info.assert_called_once()
    calls.ipc_stop_server.assert_awaited_once()


@pytest.mark.parametrize("preview", [True, False])
def test_run_ui_server_writes_server_info_only_in_the_normal_profile(monkeypatch, preview):
    calls = MagicMock()
    monkeypatch.setattr(server, "configure_logging", calls.configure_logging)
    monkeypatch.setattr(server.uvicorn, "Config", calls.uvicorn_config)
    monkeypatch.setattr(server.app.state, "uvicorn_server", None, raising=False)
    monkeypatch.setattr(server, "write_server_info", calls.write_server_info)
    monkeypatch.setattr(server, "clear_server_info", calls.clear_server_info)
    monkeypatch.setattr(server, "ArtemisUvicornServer", lambda _config: calls.uvicorn_server)
    monkeypatch.setattr(server, "PREVIEW_PROFILE", preview)
    monkeypatch.setattr(server, "configure_logging", lambda **_kwargs: None)  # process-global

    server.run_ui_server("127.0.0.1", 8123)

    calls.uvicorn_server.run.assert_called_once()
    assert calls.write_server_info.called is not preview
    assert calls.clear_server_info.called is not preview


_IMPORT_PROBE = textwrap.dedent(
    """
    import json, sys
    called = []

    def hook(name):
        def run(*_a, **_k):
            called.append(name)
        return run

    import artemis.config as config
    config.init_ls_address = hook("init_ls_address")
    import artemis.runtime as runtime
    runtime.write_server_info = hook("write_server_info")
    runtime.start_awake_service = hook("start_awake_service")
    runtime.DeviceExecutionLock.cleanup_stale_locks = classmethod(
        lambda cls, *a, **k: called.append("cleanup_stale_locks")
    )
    import apps.admin_console.server as server
    print(json.dumps({"called": called, "token": server.LIFECYCLE_TOKEN,
                      "preview": server.PREVIEW_PROFILE}))
    """
)


def _import_server(tmp_path, *, preview: bool, access_env=None) -> dict:
    env = {
        **os.environ,
        "ARTEMIS_APP_DIR": str(tmp_path / "app"),
        "TMPDIR": str(tmp_path),
        "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
        "ARTEMIS_LIFECYCLE_TOKEN": "live-token",
    }
    env.pop(ENV_PREVIEW_PROFILE, None)
    if preview:
        env[ENV_PREVIEW_PROFILE] = "1"
        env.update(access_env)
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_preview_import_runs_no_effect_and_ignores_the_live_lifecycle_token(
    tmp_path, preview_access_env
):
    outcome = _import_server(tmp_path, preview=True, access_env=preview_access_env)

    assert outcome["preview"] is True
    assert outcome["called"] == []
    assert outcome["token"] != "live-token"
    assert len(outcome["token"]) >= 32


def test_normal_import_still_initialises_the_ls_address_and_honours_the_token(tmp_path):
    outcome = _import_server(tmp_path, preview=False)

    assert outcome["preview"] is False
    assert outcome["called"] == ["init_ls_address"]
    assert outcome["token"] == "live-token"
