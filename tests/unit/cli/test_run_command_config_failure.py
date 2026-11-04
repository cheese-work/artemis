"""Characterization test for ``execute_task`` config failures (CHE-1156, audit of CHE-1089 / #53).

When configuration fails and recording that failure also fails, the user must
still see the original configuration error, and no agent or device work may
start. Negative control is recorded in the PR description.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from artemis.interfaces.cli.commands import run as run_module


@pytest.mark.asyncio
async def test_config_failure_survives_sqlite_failure_while_recording_outcome(monkeypatch):
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    monkeypatch.delenv("ARTEMIS_CLOUD_SESSION_ID", raising=False)
    config_failure = ValueError("no usable model credentials")
    recording_failure = sqlite3.OperationalError("database is locked")

    agent_class = MagicMock(name="Agent")
    record_manifest = MagicMock(name="record_attempt_manifest")
    select_device = MagicMock(name="device_pool.select_device")
    from artemis.runtime import device_pool

    with (
        patch.object(run_module, "initialize_llm_config", side_effect=config_failure),
        patch.object(run_module, "finish_trace", side_effect=recording_failure) as finish_trace,
        patch.object(run_module, "Agent", agent_class),
        patch.object(run_module, "record_attempt_manifest", record_manifest),
        patch.object(device_pool, "select_device", select_device),
        patch.object(run_module, "logger") as log,
    ):
        with pytest.raises(ValueError) as raised:
            await run_module.execute_task(goal="do the thing", session_id="session-under-test")

    assert raised.value is config_failure  # not the sqlite error, not wrapped
    finish_trace.assert_called_once_with("session-under-test", "failed", error=str(config_failure))
    log.exception.assert_called_once()
    assert "session-under-test" in log.exception.call_args.args[1:]
    agent_class.assert_not_called()
    record_manifest.assert_not_called()
    select_device.assert_not_called()
