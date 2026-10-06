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

"""Deploy-drain contract (CHE-1107): admission closes, execution continues.

Everything runs against the in-memory queue: no ADB, browser, USB or device.
"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.server import app
from apps.admin_console.services.task_queue_service import ServerDraining, TaskQueueService
from artemis.runtime import DeviceExecutionLock, trace_store

DRAIN = "/api/system/drain"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(session_repo, "db_path", tmp_path / "sessions.db")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.draining = False
    reserve = MagicMock(return_value="ticket")
    monkeypatch.setattr(DeviceExecutionLock, "reserve", reserve)
    monkeypatch.setattr(DeviceExecutionLock, "cancel_reservation", MagicMock())
    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(
        TaskQueueService, "_reject_unavailable_device", AsyncMock(return_value=None)
    )
    yield reserve
    state.queue_items.clear()
    state.active_runs.clear()
    state.executing_run_keys.clear()
    state.draining = False


def _client(peer=("127.0.0.1", 50000), base_url="http://localhost") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=peer), base_url=base_url)


def _session_rows() -> int:
    with sqlite3.connect(session_repo.db_path) as conn:
        try:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def _item(session_id: str, status: str = "pending") -> dict:
    return {"session_id": session_id, "goal": "g", "status": status, "profile": "flash"}


async def _enqueue(goals=("Open Settings",), **kwargs):
    kwargs.setdefault("device_serial", "emulator-5554")
    return await TaskQueueService.enqueue_tasks(list(goals), **kwargs)


# -- endpoint contract -----------------------------------------------------


@pytest.mark.asyncio
async def test_get_reports_idle_by_default(env):
    async with _client() as client:
        response = await client.get(DRAIN)

    assert response.status_code == 200
    assert response.json() == {"draining": False, "active_run_count": 0}


@pytest.mark.asyncio
async def test_post_enables_idempotently_and_delete_clears(env):
    async with _client() as client:
        first = await client.post(DRAIN)
        second = await client.post(DRAIN)
        reported = await client.get(DRAIN)
        cleared = await client.delete(DRAIN)
        cleared_again = await client.delete(DRAIN)
        after = await client.get(DRAIN)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"draining": True, "active_run_count": 0}
    assert reported.json() == {"draining": True, "active_run_count": 0}
    assert cleared.json() == cleared_again.json() == after.json()
    assert after.json() == {"draining": False, "active_run_count": 0}


@pytest.mark.asyncio
async def test_get_never_changes_drain_state(env):
    async with _client() as client:
        await client.get(DRAIN)
        assert state.draining is False
        await client.post(DRAIN)
        await client.get(DRAIN)
        assert state.draining is True


# -- active_run_count ------------------------------------------------------


def test_pending_and_running_items_count(env):
    state.queue_items.extend([_item("a"), _item("b", "running")])

    assert TaskQueueService.active_run_count() == 2


def test_one_session_is_never_counted_twice(env):
    state.queue_items.append(_item("a", "running"))
    state.active_runs["a"] = {"process": None, "device_id": "d"}
    state.executing_run_keys.add("a")

    assert TaskQueueService.active_run_count() == 1


def test_dispatched_run_without_queue_row_still_counts(env):
    state.executing_run_keys.add("a")
    state.active_runs["b"] = {"process": None, "device_id": "d"}

    assert TaskQueueService.active_run_count() == 2


@pytest.mark.asyncio
async def test_run_counts_until_cleanup_completes(env):
    session_id = "run-1"
    assert session_repo.create_queued_session(session_id, "", "flash", None, None, None)
    state.queue_items.append(_item(session_id, "running") | {"goal": ""})
    seen: list[int] = []

    async def cleanup(_output_task):
        # Cleanup is under way: the queue row vanishing must not end the count.
        seen.append(TaskQueueService.active_run_count())
        state.queue_items.clear()
        state.active_runs.clear()
        seen.append(TaskQueueService.active_run_count())

    with patch.object(TaskQueueService, "_finish_output_forwarder", cleanup):
        await TaskQueueService._execute_task_item(state.queue_items[0])

    assert seen == [1, 1]
    assert TaskQueueService.active_run_count() == 0
    assert state.executing_run_keys == set()


@pytest.mark.asyncio
async def test_dispatched_run_counts_after_stop_removes_row_before_it_starts(env):
    """A targeted stop drops the queue row while the coroutine has not run yet."""
    state.queue_items.append(_item("a"))
    release = asyncio.Event()

    async def fake_execute(task_item):
        await release.wait()

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()
        # Dispatched, coroutine not yet started: the stop removes the row now.
        state.queue_items.clear()

        assert TaskQueueService.active_run_count() == 1
        await asyncio.sleep(0)
        assert TaskQueueService.active_run_count() == 1

        release.set()
        await asyncio.gather(*TaskQueueService._run_tasks)

    assert TaskQueueService.active_run_count() == 0


@pytest.mark.asyncio
async def test_run_cancelled_before_it_starts_counts_until_the_task_is_done(env):
    state.queue_items.append(_item("a"))

    async def fake_execute(task_item):
        raise AssertionError("never reached: cancelled before the first step")

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()
        (run_task,) = TaskQueueService._run_tasks
        state.queue_items.clear()
        run_task.cancel()

        assert TaskQueueService.active_run_count() == 1
        await asyncio.gather(run_task, return_exceptions=True)
        await asyncio.sleep(0)

    assert TaskQueueService.active_run_count() == 0
    assert state.executing_run_keys == set()


@pytest.mark.asyncio
async def test_drain_report_counts_waiting_and_running_rows_together(env):
    """Deploy drain waits for queued work too; host maintenance must not reuse this count."""
    state.queue_items.extend([_item("w1"), _item("w2"), _item("r1", "running")])

    async with _client() as client:
        response = await client.post(DRAIN)

    assert response.json() == {"draining": True, "active_run_count": 3}


@pytest.mark.asyncio
async def test_drain_flag_does_not_fence_a_limit_n_dispatch(env, monkeypatch):
    """Drain closes submission only; the dispatcher keeps starting accepted rows."""
    monkeypatch.setattr(TaskQueueService, "_concurrency_limit", classmethod(lambda cls: 2))
    state.draining = True
    state.queue_items.extend([_item("a") | {"device_serial": "d1"}, _item("b")])
    started: list[str] = []

    async def fake_execute(task_item):
        started.append(task_item["session_id"])

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()
        await asyncio.sleep(0)

    assert started == ["a", "b"]


# -- admission closes, execution does not ---------------------------------


@pytest.mark.asyncio
async def test_pending_work_still_dispatches_while_draining(env):
    state.draining = True
    item = _item("a")
    state.queue_items.append(item)
    started: list[str] = []

    async def fake_execute(task_item):
        started.append(task_item["session_id"])

    with patch.object(TaskQueueService, "_execute_task_item", fake_execute):
        TaskQueueService._dispatch_pending_tasks()
        await asyncio.sleep(0)

    assert item["status"] == "running"
    assert started == ["a"]


@pytest.mark.asyncio
async def test_new_submission_is_refused_without_side_effects(env):
    state.draining = True

    with pytest.raises(ServerDraining):
        await _enqueue()

    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


@pytest.mark.asyncio
async def test_whole_batch_is_refused_while_draining(env):
    state.draining = True

    with pytest.raises(ServerDraining):
        await _enqueue(["one", "two", "three"])

    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


@pytest.mark.asyncio
async def test_drain_flipped_during_device_validation_is_caught_at_enqueue(env, monkeypatch):
    async def validate_then_drain(_serial):
        state.draining = True

    monkeypatch.setattr(TaskQueueService, "_reject_unavailable_device", validate_then_drain)

    with pytest.raises(ServerDraining):
        await _enqueue()

    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


@pytest.mark.asyncio
async def test_drain_flipped_during_device_selection_is_caught_at_enqueue(env):
    from artemis.runtime import device_pool

    async def select_then_drain():
        state.draining = True
        return "emulator-5554"

    with patch.object(device_pool, "select_device_async", select_then_drain):
        with pytest.raises(ServerDraining):
            await _enqueue(device_serial=None)

    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


@pytest.mark.asyncio
async def test_submission_accepted_before_drain_is_counted_and_kept(env):
    result = await _enqueue()
    state.draining = True

    assert result["enqueued_count"] == 1
    assert TaskQueueService.active_run_count() == 1
    assert len(state.queue_items) == 1
    assert _session_rows() == 1


@pytest.mark.asyncio
async def test_accepted_retry_during_drain_returns_existing_without_enqueue(env):
    first = await _enqueue(session_id="sess-1")
    state.draining = True

    retry = await _enqueue(session_id="sess-1")

    assert first["enqueued_count"] == 1
    assert retry["enqueued_count"] == 0
    assert [t["session_id"] for t in retry["tasks"]] == ["sess-1"]
    assert len(state.queue_items) == 1
    assert env.call_count == 1
    assert _session_rows() == 1


# -- /api/run over HTTP ------------------------------------------------------


@pytest.mark.asyncio
async def test_run_endpoint_returns_503_with_retry_after(env, monkeypatch):
    probe = AsyncMock()
    monkeypatch.setattr(tasks_router.readiness_engine, "run_device_submission_probe", probe)
    state.draining = True

    async with _client() as client:
        response = await client.post("/api/run", json={"goal": "Open Settings"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"]["code"] == "server_draining"
    probe.assert_not_awaited()
    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


@pytest.mark.asyncio
async def test_run_endpoint_idempotent_retry_is_not_refused(env, monkeypatch):
    state.queue_items.append(_item("sess-9", "running"))
    probe = AsyncMock()
    monkeypatch.setattr(tasks_router.readiness_engine, "run_device_submission_probe", probe)
    state.draining = True

    async with _client() as client:
        response = await client.post("/api/run", json={"goal": "g", "session_id": "sess-9"})

    assert response.status_code == 200
    assert response.json()["enqueued_count"] == 0
    assert len(state.queue_items) == 1
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_endpoint_race_during_probe_is_refused_at_enqueue(env, monkeypatch):
    async def probe_then_drain(**_kwargs):
        state.draining = True

    monkeypatch.setattr(
        tasks_router.readiness_engine, "run_device_submission_probe", probe_then_drain
    )
    monkeypatch.setattr(
        tasks_router.device_pool,
        "validate_explicit_serial_async",
        AsyncMock(return_value=None),
    )

    async with _client() as client:
        response = await client.post(
            "/api/run", json={"goal": "Open Settings", "device_serial": "emulator-5554"}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "server_draining"
    assert response.headers["retry-after"] == "5"
    assert state.queue_items == []
    env.assert_not_called()
    assert _session_rows() == 0


# -- trust boundary -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
@pytest.mark.parametrize(
    ("peer", "headers", "base_url"),
    [
        pytest.param(("203.0.113.9", 4242), {}, "http://localhost", id="remote-peer"),
        pytest.param(
            ("127.0.0.1", 4242),
            {"x-forwarded-for": "203.0.113.9"},
            "http://localhost",
            id="proxied-forwarded-for",
        ),
        pytest.param(
            ("127.0.0.1", 4242),
            {"cf-connecting-ip": "203.0.113.9"},
            "http://localhost",
            id="proxied-cloudflare",
        ),
        pytest.param(("127.0.0.1", 4242), {}, "http://evil.example", id="rebound-host"),
        pytest.param(
            ("127.0.0.1", 4242),
            {"origin": "http://evil.example"},
            "http://localhost",
            id="cross-origin",
        ),
    ],
)
async def test_untrusted_requests_are_rejected_for_every_method(
    env, method, peer, headers, base_url
):
    async with _client(peer, base_url) as client:
        response = await client.request(method, DRAIN, headers=headers)

    assert response.status_code == 403
    assert state.draining is False


@pytest.mark.asyncio
async def test_same_origin_browser_request_is_accepted(env):
    async with _client() as client:
        response = await client.post(DRAIN, headers={"origin": "http://localhost"})

    assert response.status_code == 200
    assert state.draining is True
