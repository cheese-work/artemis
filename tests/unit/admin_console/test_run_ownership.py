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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.ownership import SYSTEM_PRINCIPAL
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
from artemis.runtime.device_lock import DeviceExecutionLock

# A scoped caller's run needs a device they may use: a shared one is ready.
_SHARED_READY = SimpleNamespace(
    summary="Connected", metadata={"active_device": {"serial": "emulator-5554"}}
)

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
    monkeypatch.setattr(lock, "has_owner_record", staticmethod(lambda *_a, **_k: False))
    monkeypatch.setattr(
        lock, "has_unreadable_owner_record", staticmethod(lambda: False), raising=False
    )
    monkeypatch.setattr(task_queue_service, "ensure_worker_running", MagicMock())
    monkeypatch.setattr(task_queue_service, "stop_tasks", MagicMock(return_value=True))
    monkeypatch.setattr(task_queue_service, "resume_task", MagicMock(return_value=True))
    state.queue_items.clear()
    state.active_runs.clear()
    state.active_connections.clear()
    state.active_session_id = None
    state.is_shutting_down = False  # an earlier lifecycle test may leave it set
    state.shutdown_event.clear()
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
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token, "sub": token})
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


@pytest.mark.asyncio
async def test_everyone_catalog_is_redacted_read_only_and_keeps_mine_private(cloudflare):
    other = _run(cloudflare, QA1)
    mine = _run(cloudflare, QA2)
    unowned = _run(cloudflare, None)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute(
            "UPDATE sessions SET initial_goal = ? WHERE session_id = ?",
            ('Login with password="team-secret"', other),
        )
    response = await _get(QA2, "/api/runs", scope="everyone")
    assert _run_ids(response) == {other, mine}
    assert unowned not in _run_ids(response)
    rows = {row["session_id"]: row for row in response.json()["runs"]}
    assert rows[other]["requested_by"] == QA1
    assert all(row["read_only"] for row in rows.values())
    assert "team-secret" not in response.text
    assert _run_ids(await _get(QA2, "/api/runs")) == {mine}
    shared = await _get(QA2, f"/api/runs/{other}")
    assert shared.json()["read_only"] is True
    assert "team-secret" not in shared.text
    own = await _get(QA1, f"/api/runs/{other}")
    assert own.json()["read_only"] is False
    assert "team-secret" in own.text


@pytest.mark.asyncio
async def test_everyone_scope_requires_identity_and_does_not_widen_queue(cloudflare):
    _run(cloudflare, QA1, queued=True)
    assert (await _get(None, "/api/runs", scope="everyone")).status_code == 403
    async with _client() as client:
        forged = await client.get(
            "/api/runs?scope=everyone",
            headers={"Cf-Access-Authenticated-User-Email": QA1},
        )
    assert forged.status_code == 403
    assert (await _get(QA2, "/api/status", scope="everyone")).status_code == 400
    assert (await _get(QA2, "/api/runs", scope="all")).status_code == 403
    assert (await _get(QA2, "/api/status")).json()["queue"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,query", [("fts", "zebra7*"), ("substring", "zebra7")])
async def test_team_search_does_not_match_another_owners_secret(
    cloudflare, monkeypatch, mode, query
):
    from artemis.data_engine import run_catalog

    sid = _run(cloudflare, QA1)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute(
            "UPDATE sessions SET initial_goal = ? WHERE session_id = ?",
            ('Login with password="zebra7secret"', sid),
        )
    monkeypatch.setattr(run_catalog, "search_mode", lambda conn: mode)

    assert _run_ids(await _get(QA1, "/api/runs", q=query)) == {sid}
    assert _run_ids(await _get(QA1, "/api/runs", scope="everyone", q=query)) == {sid}
    for caller in (QA2, ADMIN):
        response = await _get(caller, "/api/runs", scope="everyone", q=query)
        assert _run_ids(response) == set()
        assert response.json()["next_cursor"] is None
    assert _run_ids(await _get(QA2, "/api/runs", scope="everyone", q=query, requester=QA1)) == set()
    assert _run_ids(await _get(QA2, "/api/runs", scope="everyone", q=" ")) == {sid}


@pytest.mark.asyncio
async def test_ambiguous_prefix_excludes_non_actionable_candidates(cloudflare, monkeypatch):
    ids = [uuid.UUID(f"deadbeef-0000-4000-8000-{suffix:012d}") for suffix in (1, 2, 3)]
    monkeypatch.setattr(uuid, "uuid4", MagicMock(side_effect=ids))
    other, mine = _run(cloudflare, QA1), _run(cloudflare, QA2)
    also_mine = _run(cloudflare, QA2)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute(
            "UPDATE sessions SET initial_goal = ?",
            ('Login with password="candidate-secret"',),
        )

    response = await _get(QA2, "/api/runs/deadbeef")
    assert response.status_code == 409
    candidates = {row["session_id"]: row for row in response.json()["candidates"]}
    assert set(candidates) == {mine, also_mine}
    assert other not in candidates
    assert candidates[mine]["read_only"] is False
    assert "candidate-secret" in candidates[mine]["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [QA1, None])
async def test_session_details_redact_another_runs_goal(cloudflare, owner):
    sid = _run(cloudflare, owner)
    goal = 'Login with password="zebra7secret"'
    with sqlite3.connect(cloudflare) as conn:
        conn.execute("UPDATE sessions SET initial_goal = ? WHERE session_id = ?", (goal, sid))

    other = await _get(QA2, f"/api/sessions/{sid}")
    assert other.status_code == 200
    assert other.json()["session_id"] == sid
    assert "zebra7secret" not in other.text
    for caller in (ADMIN, owner):
        if caller is not None:
            own = await _get(caller, f"/api/sessions/{sid}")
            assert own.status_code == 200
            assert own.json()["initial_goal"] == goal


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [QA1, QA2, ADMIN, None])
async def test_bundle_ambiguous_candidates_use_the_run_owner_rule(cloudflare, monkeypatch, caller):
    ids = [
        uuid.UUID("deadbeef-0000-4000-8000-000000000003"),
        uuid.UUID("deadbeef-0000-4000-8000-000000000004"),
        uuid.UUID("deadbeef-0000-4000-8000-000000000005"),
        uuid.UUID("deadbeef-0000-4000-8000-000000000006"),
    ]
    monkeypatch.setattr(uuid, "uuid4", MagicMock(side_effect=ids))
    other, mine = _run(cloudflare, QA1), _run(cloudflare, QA2)
    also_other, also_mine = _run(cloudflare, QA1), _run(cloudflare, QA2)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute("UPDATE sessions SET initial_goal = ?", ('password="zebra7secret"',))
    if caller is None:
        monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))

    response = await _get(caller, "/api/runs/deadbeef/bundle.zip")

    assert response.status_code == 409
    candidates = {row["session_id"]: row for row in response.json()["candidates"]}
    expected = {
        QA1: {other, also_other},
        QA2: {mine, also_mine},
        ADMIN: {other, mine, also_other, also_mine},
        None: {other, mine, also_other, also_mine},
    }
    assert set(candidates) == expected[caller]
    for session_id, owner in ((other, QA1), (mine, QA2), (also_other, QA1), (also_mine, QA2)):
        if session_id not in candidates:
            continue
        assert ("zebra7secret" in candidates[session_id]["prompt"]) is (
            caller in (owner, ADMIN, None)
        )


@pytest.mark.asyncio
async def test_open_session_details_keep_the_raw_goal(env, monkeypatch):
    monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))
    sid = _run(env, None)
    goal = 'Login with password="zebra7secret"'
    with sqlite3.connect(env) as conn:
        conn.execute("UPDATE sessions SET initial_goal = ? WHERE session_id = ?", (goal, sid))
    response = await _get(None, f"/api/sessions/{sid}")
    assert response.status_code == 200
    assert response.json()["initial_goal"] == goal


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_notes", [False, True])
@pytest.mark.parametrize("suffix", ["plan", "notes"])
@pytest.mark.parametrize("caller", [QA1, QA2, ADMIN, None])
async def test_legacy_notes_without_run_ownership_are_redacted(
    cloudflare, monkeypatch, tmp_path, suffix, caller, owned_notes
):
    from apps.admin_console.routers import media

    sid = _run(cloudflare, QA2)
    traces = tmp_path / "legacy-traces"
    notes = traces / sid / "notes" if owned_notes else traces / "notes"
    notes.mkdir(parents=True)
    (notes / "task_plan.md").write_text('password="zebra7secret"', encoding="utf-8")
    monkeypatch.setitem(
        media.media_service.get_task_plan_content.__globals__, "TRACES_PATH", traces
    )
    if caller is None:
        monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))

    response = await _get(caller, f"/api/sessions/{sid}/{suffix}")

    assert response.status_code == 200
    assert ("zebra7secret" in response.text) is (
        caller in (ADMIN, None) or (owned_notes and caller == QA2)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [QA1, QA2])
async def test_sdk_get_task_keeps_session_details_compatible(cloudflare, caller):
    from artemis_client import ArtemisClient

    sid = _run(cloudflare, QA1)
    goal = 'Login with password="zebra7secret"'
    with sqlite3.connect(cloudflare) as conn:
        conn.execute("UPDATE sessions SET initial_goal = ? WHERE session_id = ?", (goal, sid))
    transport = SimpleNamespace(
        request=lambda method, path, **kwargs: asyncio.run(_get(caller, path)).json()
    )
    result = await ArtemisClient("http://localhost", transport=transport).get_task(sid)
    assert result.task_id == sid
    assert result.done and result.succeeded
    assert result.goal is not None
    assert ("zebra7secret" in result.goal) is (caller == QA1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,provider,payload",
    [
        ("events", "sessions.session_repo.lifecycle.events", [{"goal": 'password="zebra7secret"'}]),
        (
            "tree",
            "sessions.trace_repo.get_trace_tree",
            [{"payload": {"goal": 'password="zebra7secret"'}}],
        ),
        (
            "background_tasks",
            "sessions.session_repo.get_background_tasks",
            [{"description": 'password="zebra7secret"'}],
        ),
        (
            "startup_progress",
            "sessions.state.get_startup_progress",
            [{"message": 'password="zebra7secret"'}],
        ),
        (
            "steps",
            "steps.step_repo.get_session_steps",
            [{"action_description": 'password="zebra7secret"'}],
        ),
        (
            "replay_steps",
            "replay.replay_manager.get_replay_steps",
            [{"instruction": 'password="zebra7secret"'}],
        ),
        (
            "steps/1/replay_traces",
            "replay.replay_manager.get_step_replay_traces",
            [{"payload": {"goal": 'password="zebra7secret"'}}],
        ),
        ("plan", "media.media_service.get_task_plan_content", 'password="zebra7secret"'),
        ("notes", "media.media_service.get_session_notes_content", 'password="zebra7secret"'),
        (
            "checks",
            "media.media_service.get_session_checks",
            {"records": [{"message": 'password="zebra7secret"'}]},
        ),
        (
            "video",
            "media._get_session_video_sync",
            {"status": "failed", "message": 'password="zebra7secret"'},
        ),
    ],
)
async def test_shared_session_text_endpoints_redact_secrets(
    cloudflare, monkeypatch, suffix, provider, payload
):
    from importlib import import_module

    sid = _run(cloudflare, QA1)
    module_name, attribute_path = provider.split(".", 1)
    target = import_module(f"apps.admin_console.routers.{module_name}")
    attributes = attribute_path.split(".")
    for attribute in attributes[:-1]:
        target = getattr(target, attribute)
    if provider == "sessions.session_repo.lifecycle.events":
        target = type(target)
    monkeypatch.setattr(target, attributes[-1], lambda *args, **kwargs: payload)

    for caller in (QA1, ADMIN, QA2):
        response = await _get(caller, f"/api/sessions/{sid}/{suffix}")
        assert response.status_code == 200, response.text
        assert ("zebra7secret" in response.text) is (caller != QA2)


@pytest.mark.asyncio
async def test_trace_text_uses_the_recorded_owner_not_a_supplied_session(cloudflare, monkeypatch):
    from apps.admin_console.routers import steps

    other, mine = _run(cloudflare, QA1), _run(cloudflare, QA2)
    monkeypatch.setattr(steps.step_repo, "get_step_session_id", lambda *args: other)
    monkeypatch.setattr(
        steps.trace_repo,
        "get_step_traces_tree",
        lambda *args: [{"goal": 'password="zebra7secret"'}],
    )
    monkeypatch.setattr(
        steps.trace_repo,
        "get_trace_by_id",
        lambda *args, **kwargs: {
            "session_id": other,
            "payload": json.dumps({"goal": 'password="zebra7secret"'}),
        },
    )
    for caller in (QA1, ADMIN, QA2):
        await _get(caller, f"/api/sessions/{other}")  # opening the run by its full id is the link
        for path in ("/api/steps/step1/traces", f"/api/traces/trace1?session_id={mine}"):
            response = await _get(caller, path)
            assert response.status_code == 200, response.text
            assert ("zebra7secret" in response.text) is (caller != QA2)


@pytest.mark.asyncio
async def test_session_prompt_redaction_fails_closed_when_ownership_is_unavailable(
    cloudflare, monkeypatch
):
    from apps.admin_console.database.repositories.run_catalog_repository import CatalogNotReady

    sid = _run(cloudflare, QA1)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute(
            "UPDATE sessions SET initial_goal = ? WHERE session_id = ?",
            ('password="zebra7secret"', sid),
        )
    monkeypatch.setattr(run_catalog_repo, "owners", MagicMock(side_effect=CatalogNotReady()))
    response = await _get(QA2, f"/api/sessions/{sid}")
    assert response.status_code == 503
    assert "zebra7secret" not in response.text


@pytest.mark.asyncio
async def test_everyone_filters_and_pagination_do_not_expose_unowned_runs(cloudflare):
    wanted = [_run(cloudflare, QA1, status="failed") for _ in range(3)]
    _run(cloudflare, None, status="failed")
    _run(cloudflare, QA2, status="completed")
    first = await _get(QA2, "/api/runs", scope="everyone", status="failed", limit=2)
    assert len(first.json()["runs"]) == 2
    second = await _get(
        QA2,
        "/api/runs",
        scope="everyone",
        status="failed",
        limit=2,
        cursor=first.json()["next_cursor"],
    )
    assert _run_ids(first) | _run_ids(second) == set(wanted)
    assert second.json()["next_cursor"] is None


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
    response = await tasks_router.stream_events(
        session_id="all", scope=SYSTEM_PRINCIPAL if scope is None else scope
    )
    stream = response.body_iterator
    assert (await _next_event(stream))[0] == "info"
    return stream


def _emit(session_id: str) -> None:
    task_queue_service._broadcast_event(
        "startup_progress", {"session_id": session_id, "stage": "x"}
    )


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
        tasks_router.readiness_engine,
        "run_device_submission_probe",
        AsyncMock(return_value=_SHARED_READY),
    )

    assert (await _post(QA1, "/api/run", json={"goal": "x"})).status_code == 200
    assert enqueue.await_args.kwargs["requested_by"] == QA1


@pytest.mark.asyncio
async def test_submit_without_identity_gets_no_owner(cloudflare, monkeypatch):
    enqueue = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(
        tasks_router.readiness_engine,
        "run_device_submission_probe",
        AsyncMock(return_value=_SHARED_READY),
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

    assert _session_ids(response) == set()  # no identity owns nothing, not even unowned runs
    assert unowned not in response.text
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
    # The device is resolved to its run first; stop_tasks gets the authorized run only.
    task_queue_service.stop_tasks.assert_called_once_with(
        clear_all=False, session_id=sid, device_id=None, clear_pause=True
    )


@pytest.mark.asyncio
async def test_unowned_runs_can_only_be_stopped_by_an_admin(cloudflare):
    sid = _run(cloudflare, None, queued=True)

    assert (await _post(QA1, "/api/stop", json={"session_id": sid})).status_code == 403
    anonymous = await _post(None, "/api/stop", json={"session_id": sid})
    assert anonymous.status_code == 403 and anonymous.json()["code"] == "not_run_owner"
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
    # QA2's run is also running, so QA1's stop must leave the shared pause marker alone.
    task_queue_service.stop_tasks.assert_called_once_with(
        clear_all=False, session_id=mine, device_id=None, clear_pause=False
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
    # A delete now tombstones the run, so the admin deletes a second one of QA1's.
    other = _run(cloudflare, QA1)
    assert (await _post(ADMIN, f"/api/sessions/{other}/delete")).status_code == 200
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


@pytest.mark.asyncio
async def test_stale_pause_with_no_run_is_resumable_by_an_admin_only(cloudflare, monkeypatch):
    pause_file = cloudflare.parent / ".artemis_paused"
    pause_file.write_text("LLM Error: paused", encoding="utf-8")
    monkeypatch.setattr("apps.admin_console.core.state.PAUSE_FILE", pause_file)

    assert (await _post(QA1, "/api/resume")).status_code == 403
    task_queue_service.resume_task.assert_not_called()
    assert (await _post(ADMIN, "/api/resume")).status_code == 200


def test_delete_route_is_qa_tier_and_history_wipe_stays_admin_tier():
    from apps.admin_console.core.access_control import route_tier

    assert route_tier("/api/sessions/{session_id}/delete", {"POST"}) == "qa"
    assert route_tier("/api/cleanup", {"POST"}) == "admin"


# -- correction round 1 (Sol, CHANGES REQUESTED on 7a3ccc8) ---------------------------

_REAL_STOP_TASKS = task_queue_service.stop_tasks
_REAL_RESUME_TASK = task_queue_service.resume_task


@pytest.fixture
def real_service(cloudflare, tmp_path, monkeypatch):
    """The real stop/resume service with an isolated pause marker and a kill spy."""
    from apps.admin_console.services import task_queue_service as queue_module

    monkeypatch.setattr(task_queue_service, "stop_tasks", _REAL_STOP_TASKS)
    monkeypatch.setattr(task_queue_service, "resume_task", _REAL_RESUME_TASK)
    monkeypatch.setattr(queue_module, "session_repo", session_repo)
    # Immediate kill (the graceful path has its own tests), with cancel files in tmp.
    monkeypatch.setenv("ARTEMIS_CANCEL_GRACE_SECONDS", "0")
    monkeypatch.setattr("artemis.runtime.cancel_requests.get_temp_dir", lambda _sub=None: tmp_path)
    pause_file = tmp_path / ".artemis_paused"
    monkeypatch.setattr("apps.admin_console.core.state.PAUSE_FILE", pause_file)
    monkeypatch.setattr(queue_module, "PAUSE_FILE", pause_file)
    killed = MagicMock(return_value=True)
    monkeypatch.setattr(queue_module.process_supervisor, "terminate_tree_verified", killed)
    return killed, pause_file


@pytest.fixture
def real_controls(real_service, monkeypatch):
    """The real stop/resume resolvers over fake device-lock records and a kill spy."""
    from apps.admin_console.services import task_queue_service as queue_module
    from artemis.runtime.device_lock import DeviceLockOwner

    killed, pause_file = real_service
    locks: dict[str, DeviceLockOwner] = {}
    lock = queue_module.DeviceExecutionLock
    monkeypatch.setattr(lock, "get_active_owners", staticmethod(lambda: dict(locks)))
    monkeypatch.setattr(
        lock,
        "get_active_owner",
        staticmethod(
            lambda device_id=None, *_a, **_k: (
                locks.get(device_id) or next(iter(locks.values()), None)
            )
        ),
    )
    monkeypatch.setattr(lock, "has_owner_record", staticmethod(lambda device_id=None: bool(locks)))
    monkeypatch.setattr(lock, "is_active_owner", staticmethod(lambda *_a, **_k: True))
    monkeypatch.setattr(lock, "cleanup_stale_locks", staticmethod(lambda *_a, **_k: 0))

    def hold(device: str, session_id: str, pid: int) -> None:
        locks[device] = DeviceLockOwner(
            pid=pid,
            process_created_at=1234.5,
            token=f"token-{pid}",
            device_id=device,
            description="task",
            acquired_at="2026-10-05T00:00:00+00:00",
            session_id=session_id,
            ingress="frontend",
        )

    return hold, killed, pause_file


def _status_of(db, sid: str) -> str:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT status FROM sessions WHERE session_id = ?", (sid,)).fetchone()[
            0
        ]


@pytest.mark.asyncio
async def test_own_session_paired_with_a_foreign_device_cannot_stop_the_foreign_run(real_controls):
    hold, killed, _pause = real_controls
    db = run_catalog_repo.db_path
    mine = _run(db, QA1)  # QA1's historical, finished run
    theirs = _run(db, QA2, status="running")
    hold("dev-qa2", theirs, 24680)

    response = await _post(QA1, "/api/stop", json={"session_id": mine, "device_id": "dev-qa2"})

    assert response.status_code == 400 and response.json()["code"] == "ambiguous_stop_target"
    killed.assert_not_called()
    assert _status_of(db, theirs) == "running"


@pytest.mark.asyncio
async def test_device_stop_acts_on_the_run_holding_the_device_only_for_its_owner(real_controls):
    hold, killed, _pause = real_controls
    db = run_catalog_repo.db_path
    theirs = _run(db, QA2, status="running")
    hold("dev-qa2", theirs, 24680)

    denied = await _post(QA1, "/api/stop", json={"device_id": "dev-qa2"})
    assert denied.status_code == 403 and denied.json()["code"] == "not_run_owner"
    killed.assert_not_called()
    assert _status_of(db, theirs) == "running"

    allowed = await _post(QA2, "/api/stop", json={"device_id": "dev-qa2"})
    assert allowed.status_code == 200 and allowed.json()["status"] == "stopped"
    killed.assert_called_once_with(24680, 1234.5)
    assert _status_of(db, theirs) == "cancelled"


@pytest.mark.asyncio
async def test_device_stop_with_an_unattributable_lock_is_admin_only(real_controls):
    hold, killed, _pause = real_controls
    hold("dev-x", None, 24681)  # a lock record that names no session

    denied = await _post(QA1, "/api/stop", json={"device_id": "dev-x"})

    assert denied.status_code == 403
    killed.assert_not_called()


@pytest.mark.asyncio
async def test_a_historical_owned_session_id_cannot_resume_another_users_pause(real_controls):
    _hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    old = _run(db, QA1)
    theirs = _run(db, QA2, status="running", queued=True)
    state.queue_items[-1]["status"] = "running"
    state.active_session_id = theirs
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    plain = await _post(QA1, "/api/resume")
    by_id = await _post(QA1, "/api/resume", params={"session_id": old})

    assert plain.status_code == by_id.status_code == 403
    assert pause_file.exists()


@pytest.mark.asyncio
async def test_resume_of_a_mixed_owner_pause_is_admin_only_and_a_sole_owner_may_resume(
    real_controls,
):
    _hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    mine = _run(db, QA1, status="running", queued=True)
    other = _run(db, QA2, status="running", queued=True)
    for item in state.queue_items:
        item["status"] = "running"
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).status_code == 403
    assert (await _post(QA2, "/api/resume")).status_code == 403
    assert pause_file.exists()

    state.queue_items[:] = [i for i in state.queue_items if i["session_id"] == mine]
    assert (await _post(QA1, "/api/resume")).json() == {"status": "resumed"}
    assert not pause_file.exists()
    assert other  # QA2's run was never part of the resumed set


@pytest.mark.asyncio
async def test_anonymous_caller_owns_nothing_in_cloudflare_mode(cloudflare):
    _run(cloudflare, None, queued=True)
    _run(cloudflare, None, status="running", queued=True)
    state.queue_items[-1]["status"] = "running"
    unowned = _run(cloudflare, None)

    assert _session_ids(await _get(None, "/api/sessions")) == set()
    assert _run_ids(await _get(None, "/api/runs")) == set()
    assert _queue_ids(await _get(None, "/api/status")) == set()
    assert (await _post(None, "/api/stop", json={"all": True})).json() == {
        "status": "no_running_task"
    }
    assert (await _post(None, "/api/stop")).json() == {"status": "no_running_task"}
    assert (await _post(None, "/api/resume")).status_code == 403
    task_queue_service.stop_tasks.assert_not_called()
    task_queue_service.resume_task.assert_not_called()
    # the admin rule still reaches unowned runs
    assert unowned in _session_ids(await _get(ADMIN, "/api/sessions", scope="all"))


def _bg_row(db, sid: str, task_id: str, summary: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO background_tasks (task_id, session_id, summary, status, start_time, logs) "
            "VALUES (?, ?, ?, 'running', 1.0, 'secret log')",
            (task_id, sid, summary),
        )


@pytest.mark.asyncio
async def test_status_background_tasks_bind_to_the_visible_run_not_the_latest(cloudflare):
    mine = _run(cloudflare, QA1, status="running", queued=True)
    state.queue_items[-1]["status"] = "running"
    state.active_session_id = mine
    newer = _run(cloudflare, QA2)
    with sqlite3.connect(cloudflare) as conn:
        conn.execute("UPDATE sessions SET start_time = 99.0 WHERE session_id = ?", (newer,))
    _bg_row(cloudflare, mine, "t-mine", "mine")
    _bg_row(cloudflare, newer, "t-theirs", "theirs")

    body = (await _get(QA1, "/api/status")).json()

    assert body["session_id"] == mine
    assert [t["task_id"] for t in body["background_tasks"]] == ["t-mine"]
    assert "secret log" in json.dumps(body["background_tasks"])
    assert "t-theirs" not in json.dumps(body)


@pytest.mark.asyncio
async def test_list_shaped_background_events_are_filtered_per_row(cloudflare):
    from apps.admin_console.core.ownership import owner_scope

    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    stream = await _open_stream(owner_scope(AccessIdentity(QA1, False, "cloudflare"), "mine"))
    try:
        both = [
            {"task_id": "a", "session_id": two, "logs": "theirs"},
            {"task_id": "b", "session_id": one, "logs": "mine"},
            {"task_id": "c", "logs": "unattributed"},
        ]
        task_queue_service._broadcast_event("background_tasks_updated", both)
        task_queue_service._broadcast_event(
            "background_tasks_updated", [{"task_id": "z", "session_id": two}]
        )
        _emit(one)
        event, rows = await _next_event(stream)
        assert event == "background_tasks_updated"
        assert [r["task_id"] for r in rows] == ["b"]
        # the all-foreign update was dropped, so the next event is the marker
        assert (await _next_event(stream))[0] == "startup_progress"
    finally:
        await stream.aclose()


async def _open_named_stream(session_id: str, scope):
    response = await tasks_router.stream_events(session_id=session_id, scope=scope)
    stream = response.body_iterator
    assert (await _next_event(stream))[0] == "info"
    return stream


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [QA1, QA2, ADMIN, None])
async def test_named_stream_redacts_foreign_live_text(cloudflare, caller):
    from apps.admin_console.core.ownership import owner_scope

    sid = _run(cloudflare, QA1, status="running")
    scope = owner_scope(AccessIdentity(caller, caller == ADMIN, "cloudflare" if caller else "open"))
    stream = await _open_named_stream(sid, scope)
    payloads = [
        ("session_started", {"session_id": sid, "initial_goal": 'password="zebra7secret"'}),
        ("step_recorded", {"session_id": sid, "thought": 'Use password="zebra7secret"'}),
        (
            "trace_recorded",
            {
                "session_id": sid,
                "type": "tool",
                "payload": json.dumps({"args": {"password": "zebra7secret"}}),
            },
        ),
        (
            "background_tasks_updated",
            [{"session_id": sid, "goal": 'password="zebra7secret"'}],
        ),
        ("error", 'Cannot use password="zebra7secret"'),
    ]
    try:
        for event_type, payload in payloads:
            task_queue_service._broadcast_event(event_type, payload)
            received_type, received_data = await _next_event(stream)
            assert received_type == event_type
            assert ("zebra7secret" in json.dumps(received_data)) is (caller != QA2)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [QA1, QA2, ADMIN, None])
async def test_named_stream_http_redacts_cached_progress(cloudflare, monkeypatch, caller):
    sid = _run(cloudflare, QA1, status="running")
    if caller is None:
        monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))
    monkeypatch.setattr(state, "is_shutting_down", True)
    monkeypatch.setattr(
        state,
        "get_startup_progress",
        lambda session_id: [{"session_id": session_id, "message": 'password="zebra7secret"'}],
    )

    response = await _get(caller, f"/api/stream/{sid}")

    assert response.status_code == 200
    assert "event: startup_progress" in response.text
    assert ("zebra7secret" in response.text) is (caller != QA2)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", [QA1, QA2, ADMIN])
async def test_firehose_redacts_text_without_run_ownership(cloudflare, caller):
    from apps.admin_console.core.ownership import owner_scope

    scope = owner_scope(AccessIdentity(caller, caller == ADMIN, "cloudflare"))
    stream = await _open_stream(scope)
    try:
        task_queue_service._broadcast_event(
            "startup_progress", {"message": 'password="zebra7secret"'}
        )
        event_type, data = await _next_event(stream)
        assert event_type == "startup_progress"
        assert ("zebra7secret" in json.dumps(data)) is (caller == ADMIN)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_named_stream_gets_only_its_own_runs_lifecycle_events(cloudflare):
    from apps.admin_console.core.ownership import owner_scope

    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    stream = await _open_named_stream(
        one, owner_scope(AccessIdentity(QA1, False, "cloudflare"), "mine")
    )
    try:
        task_queue_service._broadcast_event(
            "session_started", {"session_id": two, "initial_goal": "private goal"}
        )
        task_queue_service._broadcast_event("session_ended", {"session_id": two, "status": "x"})
        task_queue_service._broadcast_event(
            "background_tasks_updated",
            [{"task_id": "a", "session_id": two}, {"task_id": "b", "session_id": one}],
        )
        task_queue_service._broadcast_event("session_started", {"session_id": one})
        event, rows = await _next_event(stream)
        assert event == "background_tasks_updated"
        assert [r["task_id"] for r in rows] == ["b"]
        event, data = await _next_event(stream)
        assert (event, data["session_id"]) == ("session_started", one)
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_named_stream_in_open_mode_keeps_delivering_global_lifecycle_events(env):
    response = await tasks_router.stream_events(session_id="mine", scope=SYSTEM_PRINCIPAL)
    stream = response.body_iterator
    assert (await _next_event(stream))[0] == "info"
    try:
        task_queue_service._broadcast_event("session_started", {"session_id": "someone-elses"})
        assert (await _next_event(stream))[1]["session_id"] == "someone-elses"
    finally:
        await stream.aclose()


# -- correction round 2 (Luna, CHANGES REQUESTED on 3f0d934) --------------------------


def _own_running_run(db, owner: str = QA1) -> str:
    sid = _run(db, owner, status="running", queued=True)
    state.queue_items[-1]["status"] = "running"
    return sid


@pytest.mark.asyncio
async def test_resume_is_denied_while_a_foreign_process_holds_a_device(real_controls):
    hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    _own_running_run(db)
    foreign = _run(db, QA2, status="running")  # another process; not in this queue
    hold("dev-qa2", foreign, 24680)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    response = await _post(QA1, "/api/resume")

    assert response.status_code == 403 and response.json()["code"] == "not_run_owner"
    assert pause_file.exists()


@pytest.mark.asyncio
async def test_resume_is_denied_while_an_unowned_process_holds_a_device(real_controls):
    hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    _own_running_run(db)
    hold("dev-cli", _run(db, None, status="running"), 24682)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).status_code == 403
    assert pause_file.exists()


@pytest.mark.asyncio
async def test_resume_is_denied_while_an_unnamed_lock_is_live(real_controls):
    hold, _killed, pause_file = real_controls
    _own_running_run(run_catalog_repo.db_path)
    hold("dev-x", None, 24681)  # a lock record that names no session
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).status_code == 403
    assert pause_file.exists()
    assert (await _post(ADMIN, "/api/resume")).json() == {"status": "resumed"}
    assert not pause_file.exists()


@pytest.mark.asyncio
async def test_resume_with_only_the_callers_own_locks_still_works(real_controls):
    hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    _own_running_run(db)
    other_process = _run(db, QA1, status="running")
    hold("dev-qa1", other_process, 24683)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).json() == {"status": "resumed"}
    assert not pause_file.exists()


@pytest.mark.asyncio
async def test_clear_never_stops_a_foreign_process_even_when_it_holds_a_device(real_controls):
    hold, killed, _pause = real_controls
    db = run_catalog_repo.db_path
    mine = _run(db, QA1, queued=True)
    foreign = _run(db, QA2, status="running")
    hold("dev-qa2", foreign, 24680)

    response = await _post(QA1, "/api/stop", json={"all": True})

    assert response.status_code == 200
    killed.assert_not_called()
    assert _status_of(db, foreign) == "running"
    assert _status_of(db, mine) != "running"


# -- correction round 3 (Sol, CHANGES REQUESTED on fa5effe): the shared pause marker ----


async def _pause_with_foreign_run(real_controls):
    """QA2 is running (synthetic lock) and paused; QA1 has only their own history."""
    hold, killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    foreign = _run(db, QA2, status="running")
    hold("dev-qa2", foreign, 24680)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")
    assert (await _post(QA1, "/api/resume")).status_code == 403
    return db, foreign, killed, pause_file


@pytest.mark.asyncio
async def test_stopping_a_historical_own_run_keeps_another_users_pause(real_controls):
    db, foreign, killed, pause_file = await _pause_with_foreign_run(real_controls)
    history = _run(db, QA1)

    response = await _post(QA1, "/api/stop", json={"session_id": history})

    assert response.status_code == 200
    assert pause_file.exists()
    killed.assert_not_called()
    assert _status_of(db, foreign) == "running"


@pytest.mark.asyncio
async def test_cancelling_an_owned_queued_run_keeps_another_users_pause(real_controls):
    db, _foreign, _killed, pause_file = await _pause_with_foreign_run(real_controls)
    queued = _run(db, QA1, queued=True)

    response = await _post(QA1, "/api/stop", json={"session_id": queued})

    assert response.json()["status"] == "stopped"  # the valid cancellation still happens
    assert pause_file.exists()
    assert all(item["session_id"] != queued for item in state.queue_items)


@pytest.mark.asyncio
async def test_clearing_own_runs_keeps_another_users_pause(real_controls):
    db, _foreign, _killed, pause_file = await _pause_with_foreign_run(real_controls)
    _run(db, QA1, queued=True)
    _run(db, QA1, queued=True)

    response = await _post(QA1, "/api/stop", json={"all": True})

    assert response.json()["status"] == "stopped"
    assert pause_file.exists()
    assert not state.queue_items


@pytest.mark.asyncio
async def test_stopping_own_running_run_keeps_a_mixed_owner_pause(real_controls):
    hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    mine = _own_running_run(db)
    hold("dev-qa2", _run(db, QA2, status="running"), 24680)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    response = await _post(QA1, "/api/stop", json={"session_id": mine})

    assert response.json()["status"] == "stopped"
    assert pause_file.exists()


@pytest.mark.asyncio
async def test_stop_by_the_only_owner_and_by_an_admin_still_clears_the_pause(real_controls):
    _hold, _killed, pause_file = real_controls
    db = run_catalog_repo.db_path
    mine = _own_running_run(db)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/stop", json={"session_id": mine})).json()["status"] == "stopped"
    assert not pause_file.exists()

    queued = _run(db, QA2, queued=True)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")
    assert (await _post(ADMIN, "/api/stop", json={"session_id": queued})).status_code == 200
    assert not pause_file.exists()


_LOCK_NAMES = (
    "get_active_owners",
    "get_active_owner",
    "get_queued_tasks",
    "has_owner_record",
    "has_unreadable_owner_record",
)
_REAL_LOCK_READS = {
    name: vars(DeviceExecutionLock)[name]
    for name in _LOCK_NAMES
    if name in vars(DeviceExecutionLock)
}


@pytest.fixture
def lock_dir(real_service, tmp_path, monkeypatch):
    """Real lock-file enumeration over an isolated directory."""
    directory = tmp_path / "device-locks"
    directory.mkdir()
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _sub=None: directory)
    for name, original in _REAL_LOCK_READS.items():
        monkeypatch.setattr(DeviceExecutionLock, name, original)
    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", classmethod(lambda *_a: True))
    return directory


def _write_lock(directory, name: str, session_id: str | None, pid: int) -> None:
    payload = {
        "pid": pid,
        "process_created_at": 1234.5,
        "token": f"token-{pid}",
        "device_id": name,
        "description": "task",
        "acquired_at": "2026-10-05T00:00:00+00:00",
    }
    if session_id:
        payload["session_id"] = session_id
    (directory / f"artemis-device-{name}.lock").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_readable_owned_lock_does_not_hide_an_unreadable_one(lock_dir, real_service):
    _killed, pause_file = real_service
    db = run_catalog_repo.db_path
    _own_running_run(db)
    _write_lock(lock_dir, "dev-qa1", _run(db, QA1, status="running"), 24683)
    (lock_dir / "artemis-device-broken.lock").write_text('{"pid": 24684, "tok', encoding="utf-8")
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).status_code == 403
    assert pause_file.exists()
    assert (await _post(ADMIN, "/api/resume")).json() == {"status": "resumed"}


@pytest.mark.asyncio
async def test_a_readable_owned_lock_does_not_hide_a_parsed_but_unnamed_one(lock_dir, real_service):
    _killed, pause_file = real_service
    db = run_catalog_repo.db_path
    _own_running_run(db)
    _write_lock(lock_dir, "dev-qa1", _run(db, QA1, status="running"), 24683)
    _write_lock(lock_dir, "dev-unnamed", None, 24685)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).status_code == 403
    assert pause_file.exists()


@pytest.mark.asyncio
async def test_only_readable_owned_locks_let_the_owner_resume(lock_dir, real_service):
    _killed, pause_file = real_service
    db = run_catalog_repo.db_path
    _own_running_run(db)
    _write_lock(lock_dir, "dev-qa1", _run(db, QA1, status="running"), 24683)
    pause_file.write_text("LLM Error: paused", encoding="utf-8")

    assert (await _post(QA1, "/api/resume")).json() == {"status": "resumed"}
    assert not pause_file.exists()


# -- owner on listed sessions (CHE-1152, slice O2: the UI's "Owner" label) -----------


@pytest.mark.asyncio
async def test_sessions_carry_their_owner_so_an_admin_can_label_every_row(cloudflare):
    one, two = _run(cloudflare, QA1), _run(cloudflare, QA2)
    unowned = _run(cloudflare, None)

    rows = (await _get(ADMIN, "/api/sessions", scope="all")).json()

    assert {row["session_id"]: row["requested_by"] for row in rows} == {
        one: QA1,
        two: QA2,
        unowned: None,
    }


@pytest.mark.asyncio
async def test_a_qas_own_sessions_name_the_qa_as_owner(cloudflare):
    one = _run(cloudflare, QA1)
    _run(cloudflare, QA2)

    rows = (await _get(QA1, "/api/sessions")).json()

    assert [(row["session_id"], row["requested_by"]) for row in rows] == [(one, QA1)]


# -- cancel-queued follows the same owner-or-admin rule as stop (CHE-1128) ----------


@pytest.mark.asyncio
async def test_cancel_queued_by_a_non_owner_is_denied_with_no_side_effect(cloudflare):
    sid = _run(cloudflare, QA1, status="queued", queued=True)

    response = await _post(QA2, f"/api/tasks/{sid}/cancel-queued")

    assert response.status_code == 403 and response.json()["code"] == "not_run_owner"
    assert [i["session_id"] for i in state.queue_items] == [sid]
    assert session_repo.get_session_by_id(sid)["status"] == "queued"


@pytest.mark.asyncio
async def test_owner_and_admin_may_cancel_a_queued_run(cloudflare):
    mine = _run(cloudflare, QA1, status="queued", queued=True)
    theirs = _run(cloudflare, QA2, status="queued", queued=True)

    owner = await _post(QA1, f"/api/tasks/{mine}/cancel-queued")
    admin = await _post(ADMIN, f"/api/tasks/{theirs}/cancel-queued")

    assert owner.json()["status"] == "cancelled"
    assert admin.json()["status"] == "cancelled"
    assert state.queue_items == []


@pytest.mark.asyncio
async def test_unowned_queued_runs_can_only_be_cancelled_by_an_admin(cloudflare):
    sid = _run(cloudflare, None, status="queued", queued=True)

    assert (await _post(QA1, f"/api/tasks/{sid}/cancel-queued")).status_code == 403
    assert (await _post(None, f"/api/tasks/{sid}/cancel-queued")).status_code == 403
    assert [i["session_id"] for i in state.queue_items] == [sid]
    assert (await _post(ADMIN, f"/api/tasks/{sid}/cancel-queued")).status_code == 200


# -- a "starting" run (host agent flag on) is in flight for every owner rule (CHE-1128) --


def _starting(db, owner: str) -> str:
    sid = _run(db, owner, status="queued", queued=True)
    state.queue_items[-1]["status"] = "starting"
    return sid


def test_active_session_ids_count_a_starting_run_as_running(env):
    sid = _starting(env, QA1)

    assert sid in task_queue_service.active_session_ids(running_only=True)
    assert sid in task_queue_service.active_session_ids()


@pytest.mark.asyncio
async def test_untargeted_stop_by_the_owner_reaches_their_starting_run(cloudflare):
    sid = _starting(cloudflare, QA1)

    response = await _post(QA1, "/api/stop", json={})

    assert response.json()["status"] == "stopped"
    assert [c.kwargs["session_id"] for c in task_queue_service.stop_tasks.call_args_list] == [sid]


@pytest.mark.asyncio
async def test_clear_all_by_the_owner_includes_their_starting_run(cloudflare):
    sid = _starting(cloudflare, QA1)
    _run(cloudflare, QA2, status="queued", queued=True)

    response = await _post(QA1, "/api/stop", json={"all": True})

    assert response.json()["status"] == "stopped"
    assert [c.kwargs["session_id"] for c in task_queue_service.stop_tasks.call_args_list] == [sid]


@pytest.mark.asyncio
async def test_owner_cancel_queued_with_an_unreadable_session_asks_for_a_retry(
    cloudflare, monkeypatch
):
    sid = _run(cloudflare, QA1, status="queued", queued=True)
    monkeypatch.setattr(
        session_repo, "read_session", MagicMock(side_effect=sqlite3.OperationalError("locked"))
    )

    response = await _post(QA1, f"/api/tasks/{sid}/cancel-queued")

    assert response.status_code == 503 and response.headers["retry-after"] == "1"
    assert [i["session_id"] for i in state.queue_items] == [sid]
    assert session_repo.get_session_by_id(sid)["status"] == "queued"
