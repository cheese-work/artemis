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

"""Run ownership at the server API seam (CHE-1151, slice O1 of CHE-1150).

Each run is owned by the verified Cloudflare identity that submitted it
(``run_meta.requested_by``). Listings, the queue and event streams are scoped
to the caller; stop, resume, delete and clear need the owner or an admin;
get-by-id stays open to any signed-in user (share links). Open mode never
filters. Section one pins today's unfiltered behaviour in open mode; the rest
runs in cloudflare mode with two QAs and one admin.

A test token *is* the email: the verifier fake echoes it back as the claim.
"""

import asyncio
import json
import sqlite3
import uuid
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import AccessConfig, AccessIdentity
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import sessions as sessions_router
from apps.admin_console.routers import tasks as tasks_router
from apps.admin_console.server import app
from apps.admin_console.services.task_queue_service import task_queue_service
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store

QA1 = "qa1@example.com"
QA2 = "qa2@example.com"
ADMIN = "admin@example.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    monkeypatch.setattr(session_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(
        sessions_router.media_service, "build_video_index", lambda: {}, raising=True
    )
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", lambda *_a, **_k: None, raising=True
    )
    # No real device locks, workers or process control in these tests.
    lock = tasks_router.DeviceExecutionLock
    monkeypatch.setattr(lock, "get_queued_tasks", staticmethod(lambda: []))
    monkeypatch.setattr(lock, "get_active_owners", staticmethod(lambda: {}))
    monkeypatch.setattr(lock, "get_active_owner", staticmethod(lambda *_a, **_k: None))
    monkeypatch.setattr(task_queue_service, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(task_queue_service, "stop_tasks", MagicMock(return_value=True))
    monkeypatch.setattr(task_queue_service, "resume_task", MagicMock(return_value=True))
    state.queue_items.clear()
    state.active_runs.clear()
    state.active_connections.clear()
    state.active_session_id = None
    yield db
    state.queue_items.clear()
    state.active_runs.clear()
    state.active_session_id = None


def _use_cloudflare(monkeypatch) -> None:
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({ADMIN}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token})
    monkeypatch.setattr(app.state, "access_verifier", verifier)


@pytest.fixture
def cloudflare(env, monkeypatch):
    _use_cloudflare(monkeypatch)
    return env


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
        base_url="http://localhost",
    )


def _as(email: str) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": email}


def _run(db, owner: str | None, *, status="completed", queued=False, device=None) -> str:
    """A persisted session owned by ``owner``; ``queued`` also puts it on the queue."""
    sid = str(uuid.uuid4())
    assert session_repo.create_queued_session(sid, f"goal of {owner}", "flash", device, 1.0)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET status = ? WHERE session_id = ?", (status, sid))
        conn.execute("UPDATE run_meta SET requested_by = ? WHERE session_id = ?", (owner, sid))
    if queued:
        state.queue_items.append(
            {"session_id": sid, "goal": f"goal of {owner}", "status": "pending"}
        )
    if device:
        state.active_runs[sid] = {"device_id": device}
    return sid


async def _get(email: str | None, path: str, **params):
    async with _client() as client:
        return await client.get(path, params=params, headers=_as(email) if email else {})


async def _post(email: str | None, path: str, **kwargs):
    async with _client() as client:
        return await client.post(path, headers=_as(email) if email else {}, **kwargs)


def _session_ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["session_id"] for row in response.json()}


def _run_ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["session_id"] for row in response.json()["runs"]}


def _queue_ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["session_id"] for item in response.json()["queue"]}


# -- regression: open mode keeps today's unfiltered behaviour ------------------


@pytest.mark.asyncio
async def test_open_mode_lists_every_session_and_run_unfiltered(env):
    one, two = _run(env, QA1), _run(env, QA2)
    unowned = _run(env, None)

    async with _client() as client:
        sessions = await client.get("/api/sessions")
        runs = await client.get("/api/runs")

    assert _session_ids(sessions) == {one, two, unowned}
    assert _run_ids(runs) == {one, two, unowned}


@pytest.mark.asyncio
async def test_open_mode_queue_shows_every_pending_item(env):
    one = _run(env, QA1, queued=True)
    two = _run(env, QA2, queued=True)

    async with _client() as client:
        response = await client.get("/api/status")

    assert _queue_ids(response) == {one, two}


@pytest.mark.asyncio
async def test_open_mode_stop_resume_and_clear_pass_straight_through(env):
    other = _run(env, QA1, queued=True)

    async with _client() as client:
        stop = await client.post("/api/stop", json={"session_id": other})
        clear = await client.post("/api/stop", json={"all": True})
        resume = await client.post("/api/resume")

    assert stop.status_code == clear.status_code == resume.status_code == 200
    assert task_queue_service.stop_tasks.call_args_list[0].kwargs == {
        "clear_all": False,
        "session_id": other,
        "device_id": None,
    }
    assert task_queue_service.stop_tasks.call_args_list[1].kwargs["clear_all"] is True
    task_queue_service.resume_task.assert_called_once_with()


@pytest.mark.asyncio
async def test_open_mode_submit_sets_no_owner(env, monkeypatch):
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(
        tasks_router.readiness_engine, "run_device_submission_probe", AsyncMock(return_value=None)
    )

    async with _client() as client:
        response = await client.post("/api/run", json={"goal": "x"})

    assert response.status_code == 200
    assert enqueue.await_args.kwargs.get("requested_by") is None


async def _next_event(stream) -> tuple[str, dict]:
    chunk = await asyncio.wait_for(stream.__anext__(), timeout=2)
    lines = chunk.strip().splitlines()
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


async def _open_stream(scope=None):
    kwargs = {} if scope is None else {"scope": scope}
    response = await tasks_router.stream_events(session_id="all", **kwargs)
    stream = response.body_iterator
    assert (await _next_event(stream))[0] == "info"
    return stream


def _emit(session_id: str) -> None:
    task_queue_service._broadcast_event("startup_progress", {"session_id": session_id, "stage": "x"})


@pytest.mark.asyncio
async def test_open_mode_stream_delivers_every_session_event(env):
    stream = await _open_stream()
    try:
        _emit("someone-elses")
        _emit("mine")
        assert (await _next_event(stream))[1]["session_id"] == "someone-elses"
        assert (await _next_event(stream))[1]["session_id"] == "mine"
    finally:
        await stream.aclose()


# -- submit stamps the verified identity ------------------------------------------


@pytest.mark.asyncio
async def test_submit_records_the_verified_identity_as_requester(cloudflare, monkeypatch):
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(
        tasks_router.readiness_engine, "run_device_submission_probe", AsyncMock(return_value=None)
    )

    assert (await _post(QA1, "/api/run", json={"goal": "x"})).status_code == 200
    assert enqueue.await_args.kwargs["requested_by"] == QA1


@pytest.mark.asyncio
async def test_submit_without_identity_gets_no_owner(cloudflare, monkeypatch):
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(
        tasks_router.readiness_engine, "run_device_submission_probe", AsyncMock(return_value=None)
    )

    assert (await _post(None, "/api/run", json={"goal": "x"})).status_code == 200
    assert enqueue.await_args.kwargs["requested_by"] is None


@pytest.mark.asyncio
async def test_enqueue_persists_owner_on_run_meta_and_queue_item(cloudflare, monkeypatch):
    from unittest.mock import patch

    queue_module = __import__(
        "apps.admin_console.services.task_queue_service", fromlist=["task_queue_service"]
    )
    monkeypatch.setattr(queue_module, "session_repo", session_repo)
    sid = str(uuid.uuid4())
    with (
        patch.object(queue_module.DeviceExecutionLock, "reserve", return_value="ticket"),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device_async",
            new=AsyncMock(return_value="emulator-5554"),
        ),
    ):
        result = await task_queue_service.enqueue_tasks(["g"], session_id=sid, requested_by=QA1)

    assert result["tasks"][0]["requested_by"] == QA1
    with sqlite3.connect(cloudflare) as conn:
        row = conn.execute(
            "SELECT requested_by FROM run_meta WHERE session_id = ?", (sid,)
        ).fetchone()
    assert row == (QA1,)


@pytest.mark.asyncio
async def test_resubmitting_someone_elses_session_id_is_denied(cloudflare, monkeypatch):
    sid = _run(cloudflare, QA1, queued=True)
    enqueue = AsyncMock()
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)

    denied = await _post(QA2, "/api/run", json={"goal": "x", "session_id": sid})
    retry = await _post(QA1, "/api/run", json={"goal": "x", "session_id": sid})

    assert denied.status_code == 403 and denied.json()["code"] == "not_run_owner"
    assert "goal of" not in denied.text
    assert retry.status_code == 200 and retry.json()["enqueued_count"] == 0
    enqueue.assert_not_awaited()


# -- list, queue and stream scoping -----------------------------------------------


@pytest.mark.asyncio
async def test_each_qa_lists_only_their_own_sessions_and_runs(cloudflare):
    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    _run(cloudflare, None)

    assert _session_ids(await _get(QA1, "/api/sessions")) == {one}
    assert _session_ids(await _get(QA2, "/api/sessions")) == {two}
    assert _run_ids(await _get(QA1, "/api/runs")) == {one}
    assert _run_ids(await _get(QA2, "/api/runs")) == {two}


@pytest.mark.asyncio
async def test_requester_filter_cannot_widen_a_qas_scope(cloudflare):
    _run(cloudflare, QA1)
    two = _run(cloudflare, QA2)

    assert _run_ids(await _get(QA1, "/api/runs", requester=QA2)) == set()
    assert _run_ids(await _get(ADMIN, "/api/runs", scope="all", requester=QA2)) == {two}


@pytest.mark.asyncio
async def test_admin_sees_every_run_including_unowned_with_scope_all(cloudflare):
    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    unowned = _run(cloudflare, None)
    mine = _run(cloudflare, ADMIN)

    assert _session_ids(await _get(ADMIN, "/api/sessions", scope="all")) == {
        one,
        two,
        unowned,
        mine,
    }
    assert _run_ids(await _get(ADMIN, "/api/runs", scope="all")) == {one, two, unowned, mine}
    assert _session_ids(await _get(ADMIN, "/api/sessions")) == {mine}


@pytest.mark.asyncio
async def test_scope_all_is_admin_only_and_scope_is_validated(cloudflare):
    denied = await _get(QA1, "/api/sessions", scope="all")
    bad = await _get(QA1, "/api/sessions", scope="everything")

    assert denied.status_code == 403 and denied.json()["code"] == "scope_all_requires_admin"
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_scope"


@pytest.mark.asyncio
async def test_forged_email_headers_without_a_token_are_ignored(cloudflare):
    _run(cloudflare, QA1)
    unowned = _run(cloudflare, None)
    forged = {"Cf-Access-Authenticated-User-Email": QA1, "X-Forwarded-Email": QA1}

    async with _client() as client:
        response = await client.get("/api/sessions", headers=forged)
        admin_forged = await client.get(
            "/api/sessions", params={"scope": "all"}, headers={"X-Forwarded-Email": ADMIN}
        )

    assert _session_ids(response) == {unowned}
    assert admin_forged.status_code == 403


@pytest.mark.asyncio
async def test_each_qa_sees_only_their_own_queue(cloudflare, monkeypatch):
    one = _run(cloudflare, QA1, queued=True)
    two = _run(cloudflare, QA2, queued=True)
    unowned = _run(cloudflare, None, queued=True)
    monkeypatch.setattr(
        tasks_router.DeviceExecutionLock,
        "get_queued_tasks",
        staticmethod(lambda: [{"session_id": two}]),
    )

    assert _queue_ids(await _get(QA1, "/api/status")) == {one}
    assert _queue_ids(await _get(QA2, "/api/status")) == {two}
    assert _queue_ids(await _get(ADMIN, "/api/status")) == set()
    assert _queue_ids(await _get(ADMIN, "/api/status", scope="all")) == {one, two, unowned}


@pytest.mark.asyncio
async def test_status_hides_another_users_running_goal(cloudflare):
    theirs = _run(cloudflare, QA1, status="running", queued=True, device="emulator-5554")
    state.queue_items[-1]["status"] = "running"
    state.active_session_id = theirs

    mine = (await _get(QA1, "/api/status")).json()
    other = (await _get(QA2, "/api/status")).json()

    assert mine["session_id"] == theirs and mine["goal"]
    assert other["session_id"] is None and not other["goal"]
    assert other["queue"] == [] and other["active_tasks"] == []


@pytest.mark.asyncio
async def test_each_qa_streams_only_their_own_events(cloudflare):
    from apps.admin_console.core.ownership import owner_scope

    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    stream = await _open_stream(owner_scope(AccessIdentity(QA1, False, "cloudflare"), "mine"))
    try:
        _emit(two)
        _emit(one)
        assert (await _next_event(stream))[1]["session_id"] == one
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_admin_stream_with_scope_all_sees_every_event(cloudflare):
    from apps.admin_console.core.ownership import owner_scope

    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    stream = await _open_stream(owner_scope(AccessIdentity(ADMIN, True, "cloudflare"), "all"))
    try:
        _emit(two)
        _emit(one)
        assert (await _next_event(stream))[1]["session_id"] == two
        assert (await _next_event(stream))[1]["session_id"] == one
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_stream_endpoint_rejects_scope_all_for_a_qa(cloudflare):
    denied = await _get(QA1, "/api/stream", scope="all")
    assert denied.status_code == 403 and denied.json()["code"] == "scope_all_requires_admin"


@pytest.mark.asyncio
async def test_link_access_by_id_stays_open_to_any_signed_in_user(cloudflare):
    sid = _run(cloudflare, QA1)

    for path in (f"/api/sessions/{sid}", f"/api/runs/{sid}"):
        response = await _get(QA2, path)
        assert response.status_code == 200, path
        assert response.json()["session_id"] == sid


# -- owner-or-admin actions --------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_by_a_non_owner_is_denied_with_no_side_effect(cloudflare):
    sid = _run(cloudflare, QA1, queued=True)

    response = await _post(QA2, "/api/stop", json={"session_id": sid})

    assert response.status_code == 403 and response.json()["code"] == "not_run_owner"
    task_queue_service.stop_tasks.assert_not_called()


@pytest.mark.asyncio
async def test_owner_and_admin_may_stop_a_run(cloudflare):
    sid = _run(cloudflare, QA1, queued=True)

    assert (await _post(QA1, "/api/stop", json={"session_id": sid})).status_code == 200
    assert (await _post(ADMIN, "/api/stop", json={"session_id": sid})).status_code == 200
    assert [c.kwargs["session_id"] for c in task_queue_service.stop_tasks.call_args_list] == [
        sid,
        sid,
    ]


@pytest.mark.asyncio
async def test_stop_by_device_resolves_the_run_owner(cloudflare):
    sid = _run(cloudflare, QA1, status="running", device="dev-qa1")

    denied = await _post(QA2, "/api/stop", json={"device_id": "dev-qa1"})
    allowed = await _post(QA1, "/api/stop", json={"device_id": "dev-qa1"})

    assert denied.status_code == 403
    assert allowed.status_code == 200
    task_queue_service.stop_tasks.assert_called_once_with(
        clear_all=False, session_id=None, device_id="dev-qa1"
    )
    assert sid in state.active_runs


@pytest.mark.asyncio
async def test_unowned_runs_can_only_be_stopped_by_an_admin(cloudflare):
    sid = _run(cloudflare, None, queued=True)

    assert (await _post(QA1, "/api/stop", json={"session_id": sid})).status_code == 403
    task_queue_service.stop_tasks.assert_not_called()
    assert (await _post(ADMIN, "/api/stop", json={"session_id": sid})).status_code == 200


@pytest.mark.asyncio
async def test_clear_by_a_qa_cancels_only_their_own_runs(cloudflare):
    mine_a = _run(cloudflare, QA1, queued=True)
    mine_b = _run(cloudflare, QA1, queued=True)
    _run(cloudflare, QA2, queued=True)
    _run(cloudflare, None, queued=True)

    response = await _post(QA1, "/api/stop", json={"all": True})

    assert response.status_code == 200
    stopped = {c.kwargs["session_id"] for c in task_queue_service.stop_tasks.call_args_list}
    assert stopped == {mine_a, mine_b}
    assert all(not c.kwargs["clear_all"] for c in task_queue_service.stop_tasks.call_args_list)


@pytest.mark.asyncio
async def test_clear_with_nothing_owned_stops_nothing(cloudflare):
    _run(cloudflare, QA2, queued=True)

    response = await _post(QA1, "/api/stop", json={"all": True})

    assert response.json() == {"status": "no_running_task"}
    task_queue_service.stop_tasks.assert_not_called()


@pytest.mark.asyncio
async def test_admin_clear_still_clears_everything(cloudflare):
    _run(cloudflare, QA2, queued=True)

    assert (await _post(ADMIN, "/api/stop", json={"all": True})).status_code == 200
    task_queue_service.stop_tasks.assert_called_once_with(
        clear_all=True, session_id=None, device_id=None
    )


@pytest.mark.asyncio
async def test_untargeted_stop_by_a_qa_only_touches_their_own_single_run(cloudflare):
    mine = _run(cloudflare, QA1, status="running", queued=True, device="dev-1")
    _run(cloudflare, QA2, status="running", queued=True, device="dev-2")

    assert (await _post(QA1, "/api/stop")).status_code == 200
    task_queue_service.stop_tasks.assert_called_once_with(
        clear_all=False, session_id=mine, device_id=None
    )
    task_queue_service.stop_tasks.reset_mock()
    assert (await _post("qa3@example.com", "/api/stop")).json() == {"status": "no_running_task"}
    task_queue_service.stop_tasks.assert_not_called()


@pytest.mark.asyncio
async def test_resume_needs_the_owner_of_the_paused_run_or_an_admin(cloudflare):
    sid = _run(cloudflare, QA1, status="running", queued=True)
    state.active_session_id = sid

    denied = await _post(QA2, "/api/resume")
    by_id = await _post(QA2, "/api/resume", params={"session_id": sid})
    assert denied.status_code == by_id.status_code == 403
    assert denied.json()["code"] == "not_run_owner"
    task_queue_service.resume_task.assert_not_called()

    assert (await _post(QA1, "/api/resume")).status_code == 200
    assert (await _post(ADMIN, "/api/resume")).status_code == 200
    assert task_queue_service.resume_task.call_count == 2


@pytest.fixture
def delete_spy(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(StorageManager, "delete_session", spy)
    return spy


@pytest.mark.asyncio
async def test_delete_needs_the_owner_or_an_admin(cloudflare, delete_spy):
    sid = _run(cloudflare, QA1)

    denied = await _post(QA2, f"/api/sessions/{sid}/delete")
    assert denied.status_code == 403 and denied.json()["code"] == "not_run_owner"
    delete_spy.assert_not_called()

    assert (await _post(QA1, f"/api/sessions/{sid}/delete")).status_code == 200
    assert (await _post(ADMIN, f"/api/sessions/{sid}/delete")).status_code == 200
    assert delete_spy.call_count == 2


@pytest.mark.asyncio
async def test_delete_of_an_unowned_run_is_admin_only(cloudflare, delete_spy):
    sid = _run(cloudflare, None)

    assert (await _post(QA1, f"/api/sessions/{sid}/delete")).status_code == 403
    delete_spy.assert_not_called()
    assert (await _post(ADMIN, f"/api/sessions/{sid}/delete")).status_code == 200


@pytest.mark.asyncio
async def test_delete_requires_sign_in(cloudflare, delete_spy):
    sid = _run(cloudflare, None)

    response = await _post(None, f"/api/sessions/{sid}/delete")

    assert response.status_code == 401 and response.json()["code"] == "not_signed_in"
    delete_spy.assert_not_called()


@pytest.mark.asyncio
async def test_history_wipe_stays_admin_only(cloudflare):
    response = await _post(QA1, "/api/cleanup")

    assert response.status_code == 403 and response.json()["code"] == "admin_required"
