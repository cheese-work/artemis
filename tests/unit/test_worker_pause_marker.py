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

"""A worker's session start/end must not resume another live run (CHE-1151).

The pause marker is shared by every run. The console only lets an owner or an
admin clear it (see test_run_ownership.py), but an allowed submit or cancel
starts or ends a worker session, and ``DataEngine`` used to delete the marker
unconditionally there, resuming a foreign paused run. The real ``DataEngine``
and the real ``_wait_for_resume`` are used; the other run is a synthetic lock
record or session row.
"""

import asyncio
import json
import os
import sqlite3
from unittest.mock import MagicMock
import uuid

import pytest

from artemis.context import ArtemisContext
from artemis.data_engine import engine as engine_module
from artemis.data_engine.engine import DataEngine
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.services import llm

FOREIGN_PID = 24680


@pytest.fixture
def world(tmp_path, monkeypatch):
    pause_file = tmp_path / ".artemis_paused"
    monkeypatch.setattr(engine_module, "PAUSE_FILE", pause_file)
    locks = tmp_path / "device-locks"
    locks.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _sub=None: locks)
    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", classmethod(lambda *_a: True))
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = MagicMock(traces_path=str(tmp_path))
    ctx.device = None
    engine = DataEngine(ctx)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")
    return engine, pause_file, locks


def _lock(directory, name: str, pid: int, session_id: str | None = "other-run") -> None:
    payload = {
        "pid": pid,
        "process_created_at": 1234.5,
        "token": f"token-{name}",
        "device_id": name,
        "description": "task",
        "acquired_at": "2026-10-05T00:00:00+00:00",
    }
    if session_id:
        payload["session_id"] = session_id
    (directory / f"artemis-device-{name}.lock").write_text(json.dumps(payload), encoding="utf-8")


async def _still_waiting(pause_file) -> bool:
    """True if the real ``_wait_for_resume`` is still blocked after a poll interval."""
    waiter = asyncio.create_task(llm._wait_for_resume(pause_file))
    done, _ = await asyncio.wait({waiter}, timeout=1.5)
    if waiter in done:
        return not waiter.result()
    waiter.cancel()
    return True


@pytest.mark.asyncio
async def test_start_and_cancel_keep_the_pause_while_a_foreign_run_holds_a_device(world):
    engine, pause_file, locks = world
    _lock(locks, "dev-qa2", FOREIGN_PID)

    engine.start_session("separate run")
    assert pause_file.exists()
    assert await _still_waiting(pause_file)

    engine.end_session("cancelled")
    assert pause_file.exists()
    assert await _still_waiting(pause_file)


@pytest.mark.asyncio
@pytest.mark.parametrize("unattributable", ["unnamed", "unreadable"])
async def test_an_unattributable_lock_also_keeps_the_pause(world, unattributable):
    engine, pause_file, locks = world
    if unattributable == "unnamed":
        _lock(locks, "dev-x", FOREIGN_PID, session_id=None)
    else:
        (locks / "artemis-device-broken.lock").write_text('{"pid": 24680, "tok', encoding="utf-8")

    engine.start_session("run")
    engine.end_session("cancelled")

    assert pause_file.exists()
    assert await _still_waiting(pause_file)


@pytest.mark.asyncio
async def test_another_live_session_row_keeps_the_pause(world):
    engine, pause_file, _locks = world
    engine.start_session("run")
    other = str(uuid.uuid4())
    with sqlite3.connect(engine.storage.db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, pid) "
            "VALUES (?, 'other', 1.0, 'running', ?)",
            (other, os.getppid()),  # a live process that is not this worker
        )

    engine.end_session("cancelled")

    assert pause_file.exists()
    assert await _still_waiting(pause_file)


@pytest.mark.asyncio
async def test_the_sole_live_run_still_clears_the_marker(world):
    engine, pause_file, locks = world
    _lock(locks, "dev-mine", os.getpid(), session_id="mine")  # this worker's own lock

    engine.start_session("run")  # clears a leftover marker, as before
    assert not pause_file.exists()
    assert not await _still_waiting(pause_file)

    pause_file.write_text("LLM Error: paused again", encoding="utf-8")
    engine.end_session("cancelled")  # and so does ending the only live run
    assert not pause_file.exists()
    assert not await _still_waiting(pause_file)
