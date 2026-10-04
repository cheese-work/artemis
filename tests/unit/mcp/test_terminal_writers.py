"""Every MCP terminal write goes through the lifecycle authority (CHE-1089 review blocker 1)."""

import os
import shutil
import sqlite3
import tempfile
import uuid
from unittest.mock import patch

import pytest

from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store
from artemis.runtime.lifecycle import LifecycleAuthority
from mcp_server.tools.task_manager import mobile_manage_task


@pytest.fixture
def traces(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    monkeypatch.setenv("ARTEMIS_STANDALONE", "1")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def _running_task(traces_dir: str, pid: int | None = None) -> str:
    """A task with both a status.json and a running sessions row, like a live MCP run."""
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Task to stop", "Flash", "conv-1")
    db_path = os.path.join(traces_dir, "data_engine.db")
    StorageManager(db_path, traces_dir)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info, pid)"
            " VALUES (?, 'goal', 1.0, 'running', '{}', ?)",
            (trace_id, pid if pid is not None else os.getpid()),
        )
    return trace_id


def _db_status(traces_dir: str, trace_id: str) -> str:
    with sqlite3.connect(os.path.join(traces_dir, "data_engine.db")) as conn:
        return conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (trace_id,)
        ).fetchone()[0]


def test_standalone_stop_commits_cancelled_so_a_later_worker_failure_cannot_override_it(traces):
    trace_id = _running_task(traces)
    with patch("mcp_server.tools.task_manager.os.kill"):
        result = mobile_manage_task(action="stop", trace_id=trace_id)
    assert result["status"] == "cancelled"
    assert _db_status(traces, trace_id) == "cancelled"

    # The killed worker then exits non-zero: the cancellation must stand.
    outcome = LifecycleAuthority(os.path.join(traces, "data_engine.db")).settle_worker_exit(
        trace_id, returncode=1, manual_stop=False
    )

    assert outcome.status == "cancelled"
    assert trace_store.read_status(trace_id)["status"] == "cancelled"
    assert _db_status(traces, trace_id) == "cancelled"


def test_stop_of_a_trace_without_a_session_row_is_still_published_once(traces):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "No row yet", "Flash", "conv-1")
    with patch("mcp_server.tools.task_manager.os.kill"):
        trace_store.update_trace_pid(trace_id, 4242)
        result = mobile_manage_task(action="stop", trace_id=trace_id)

    assert result["status"] == "cancelled"
    assert trace_store.read_status(trace_id)["status"] == "cancelled"


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted", "success"])
def test_trace_store_update_cannot_set_an_outcome(traces, status):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "task", "Flash")

    with pytest.raises(ValueError, match="LifecycleAuthority"):
        trace_store.update_trace_status(trace_id, status)

    assert trace_store.read_status(trace_id)["status"] == "running"


def test_trace_store_update_still_serves_non_terminal_metadata(traces):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "task", "Flash")

    updated = trace_store.update_trace_status(trace_id, "running", device_serial="emulator-5554")

    assert updated["device_serial"] == "emulator-5554"
