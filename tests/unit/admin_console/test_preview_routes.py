"""Preview route allowlist and synthetic dispatch (CHE-1289).

Every registered route has a disposition; a disabled or unclassified route answers
``preview_disabled`` before any handler runs; the control routes a preview needs are
answered from preview-only state with the real ownership checks.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from apps.admin_console import server
from apps.admin_console.core import preview_routes as pr
from apps.admin_console.core.access_control import (
    AccessConfig,
    AdminAPIError,
    CloudflareAccessVerifier,
    admin_api_error_handler,
    public_tier,
)
from apps.admin_console.core.preview_profile import ENV_PREVIEW_PROFILE
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import preview_synthetic
from apps.admin_console.services.task_queue_service import task_queue_service
from artemis.core.diagnostics import readiness_engine
from artemis.data_engine.storage import StorageManager
from artemis.runtime import device_pool
from artemis.runtime.device_lock import DeviceExecutionLock

REPO_ROOT = Path(__file__).resolve().parents[3]

QA1 = "qa1@example.com"
QA2 = "qa2@example.com"
ADMIN = "admin@example.com"


# -- the allowlist covers the whole router tree -------------------------------


def test_every_registered_route_is_classified():
    assert pr.unclassified_routes(server.app) == []


def test_classifications_are_exclusive_and_name_only_registered_routes():
    assert not pr.REAL & pr.SYNTHETIC
    assert not pr.ALLOWED & pr.DISABLED
    registered = {key for route in pr.registered_routes(server.app) for key in route.keys}
    assert (pr.REAL | pr.SYNTHETIC | pr.DISABLED) - registered == set()


def test_an_unclassified_route_in_a_nested_router_fails_the_enumeration():
    child = APIRouter()

    @child.post("/rogue")
    async def rogue():
        return {}

    parent = APIRouter()
    parent.include_router(child, prefix="/nested")
    app = FastAPI()
    app.include_router(parent, prefix="/api")

    assert pr.unclassified_routes(app) == ["POST /api/nested/rogue"]
    with pytest.raises(pr.UnsupportedRouteTree, match="POST /api/nested/rogue"):
        pr.require_classified(app)


def test_a_head_only_route_stays_in_the_inventory():
    app = FastAPI()

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        return {}

    @app.head("/api/rogue-head")
    async def rogue_head():
        return {}

    assert pr.unclassified_routes(app) == ["HEAD /api/rogue-head"]
    with pytest.raises(pr.UnsupportedRouteTree, match="HEAD /api/rogue-head"):
        pr.require_classified(app)


def test_a_get_route_does_not_gain_a_head_entry():
    app = FastAPI()

    @app.get("/api/sessions")
    async def sessions():
        return []

    assert pr.unclassified_routes(app) == []


def test_a_mounted_app_is_refused_rather_than_guessed_at():
    from starlette.routing import Mount

    app = FastAPI()
    app.router.routes.append(Mount("/static", app=FastAPI()))

    with pytest.raises(pr.UnsupportedRouteTree):
        pr.registered_routes(app)


def test_normal_profile_installs_neither_the_guard_nor_the_synthetic_routes():
    assert server.PREVIEW_PROFILE is False
    assert pr.PreviewRouteGuard not in {m.cls for m in server.app.user_middleware}
    synthetic_paths = {
        route.original_route.endpoint.__module__
        for route in _effective(server.app)
        if hasattr(route, "original_route") and hasattr(route.original_route, "endpoint")
    }
    assert preview_synthetic.__name__ not in synthetic_paths


def _effective(app):
    def walk(nodes):
        for node in nodes:
            if hasattr(node, "effective_candidates"):
                yield from walk(node.effective_candidates())
            else:
                yield node

    return walk(app.router.routes)


# -- the guard refuses before any handler runs --------------------------------


@pytest.fixture
def guarded_app():
    """A toy app guarded like the preview; every handler records that it ran."""
    ran: list[str] = []
    app = FastAPI()

    @app.get("/local_file")
    async def local_file():
        ran.append("local_file")

    @app.head("/api/rogue-head")
    async def rogue_head():
        ran.append("rogue-head")

    @app.get("/api/system/config")
    async def live_config():
        ran.append("config")

    @app.post("/api/run")
    async def run_task(body: dict):
        ran.append("run")

    @app.get("/api/brand-new")
    async def brand_new():
        ran.append("brand-new")

    @app.get("/api/sessions")
    async def sessions():
        ran.append("sessions")
        return []

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        ran.append("spa")
        return {"spa": full_path}

    @app.websocket("/api/agent/connect")
    async def agent_connect(websocket: WebSocket):
        ran.append("agent")
        await websocket.accept()

    app.add_middleware(pr.PreviewRouteGuard, route_source=app)
    return app, ran


@pytest.fixture
def guarded(guarded_app):
    app, ran = guarded_app
    return TestClient(app), ran


def _refused(response) -> bool:
    return response.status_code == 403 and response.json()["code"] == pr.PREVIEW_DISABLED_CODE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/system/config"),
        ("HEAD", "/api/system/config"),
        ("GET", "/api/brand-new"),  # never classified
        ("GET", "/admin"),
        ("GET", "/admin/"),
        ("GET", "/debug/anything"),
        ("DELETE", "/api/never-registered"),
        ("HEAD", "/api/rogue-head"),  # a HEAD-only route nobody classified
        ("HEAD", "/local_file"),
    ],
)
def test_disabled_and_unclassified_routes_answer_before_the_handler(guarded, method, path):
    client, ran = guarded

    response = client.request(method, path)

    assert response.status_code == 403
    assert ran == []
    if method != "HEAD":
        assert _refused(response)


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        ("/preview/pr-92/local_file", True),  # disabled route behind the root path
        ("/preview/pr-92/admin/x", True),  # legacy console behind the root path
        ("/preview/pr-92/api/system/config", True),
        ("/preview/pr-92/api/sessions", False),
        ("/preview/pr-92-other/local_file", True),  # not under the root path: SPA fallback path
    ],
)
def test_the_guard_matches_the_path_the_router_selects_under_a_root_path(
    guarded_app, path, refused
):
    app, ran = guarded_app
    client = TestClient(app, root_path="/preview/pr-92")

    response = client.get(path)

    if refused and "pr-92-other" not in path:
        assert _refused(response)
        assert ran == []
    elif refused:
        # Outside the root path the router sees the full path, which only the SPA serves.
        assert response.status_code == 200 and ran == ["spa"]
    else:
        assert response.status_code == 200 and ran == ["sessions"]


def test_a_disabled_route_is_refused_before_its_body_is_validated(guarded):
    client, ran = guarded

    response = client.post(
        "/api/run", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert _refused(response)
    assert ran == []


def test_a_disabled_websocket_closes_before_it_is_accepted(guarded):
    client, ran = guarded

    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/agent/connect"),
    ):
        pass

    assert closed.value.code == 1008
    assert ran == []


def test_allowed_routes_and_the_spa_fallback_still_serve(guarded):
    client, ran = guarded

    assert client.get("/api/sessions").status_code == 200
    assert client.get("/deep/link/in/the/spa").json() == {"spa": "deep/link/in/the/spa"}
    assert ran == ["sessions", "spa"]


# -- synthetic dispatch: real ownership checks, preview-only state ------------


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    """The synthetic router on a toy app; every real side effect it must avoid is a trap."""
    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    monkeypatch.setattr(session_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    traps = MagicMock()
    for owner, name in [
        (task_queue_service, "stop_tasks"),
        (task_queue_service, "resume_task"),
        (task_queue_service, "cancel_queued"),
        (task_queue_service, "ensure_worker_running"),
        (task_queue_service, "active_session_ids"),
        (DeviceExecutionLock, "get_active_owner"),
        (DeviceExecutionLock, "get_active_owners"),
        (DeviceExecutionLock, "get_queued_tasks"),
    ]:
        monkeypatch.setattr(owner, name, getattr(traps, name))
    for owner, name in [
        (device_pool, "list_devices_async"),
        (readiness_engine, "run_all"),
    ]:
        monkeypatch.setattr(owner, name, AsyncMock(side_effect=AssertionError(name)))
    state.queue_items.clear()
    preview_synthetic.control.paused = False

    app = FastAPI(dependencies=[Depends(public_tier)])
    app.add_exception_handler(AdminAPIError, admin_api_error_handler)
    app.state.access_config = AccessConfig(
        auth_mode="cloudflare",
        audience="test-audience",
        issuer="https://team.cloudflareaccess.com",
        admin_emails=frozenset({ADMIN}),
    )
    verifier = MagicMock(spec=CloudflareAccessVerifier)
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token})
    app.state.access_verifier = verifier
    app.include_router(preview_synthetic.router)
    yield SimpleNamespace(client=TestClient(app), db=db, traps=traps)
    state.queue_items.clear()
    preview_synthetic.control.paused = False


def _run(env, owner, status):
    """A persisted run owned by ``owner`` plus its synthetic queue entry."""
    sid = str(uuid.uuid4())
    assert session_repo.create_queued_session(sid, f"goal of {owner}", "flash", None, 1.0)
    with sqlite3.connect(env.db) as conn:
        conn.execute("UPDATE run_meta SET requested_by = ? WHERE session_id = ?", (owner, sid))
    item = {"session_id": sid, "goal": f"goal of {owner}", "status": status}
    state.queue_items.append(item)
    return sid, item


def _as(email):
    return {"Cf-Access-Jwt-Assertion": email}


def _no_real_side_effect(env):
    assert env.traps.mock_calls == []  # every real queue/lock call hangs off the one trap mock


def test_devices_and_readiness_are_deterministic_and_empty(synthetic):
    client = synthetic.client

    assert client.get("/api/devices", headers=_as(QA1)).json() == {"devices": []}
    assert client.get("/api/stream/device-state").json() == {
        "connected": False,
        "serial": None,
        "live_stream_url": None,
    }
    report = client.get("/api/system/readiness").json()
    assert report["overall_ready"] is False
    assert report["probes"] == []
    assert report["active_device"] is None
    assert client.get("/api/system/readiness").json() == report
    _no_real_side_effect(synthetic)


def test_status_shows_each_qa_only_their_own_queue(synthetic):
    mine, _ = _run(synthetic, QA1, "pending")
    theirs, _ = _run(synthetic, QA2, "pending")
    running, _ = _run(synthetic, QA2, "running")

    own = synthetic.client.get("/api/status", headers=_as(QA1)).json()
    admin = synthetic.client.get("/api/status?scope=all", headers=_as(ADMIN)).json()

    assert [i["session_id"] for i in own["queue"]] == [mine]
    assert own["active_tasks"] == []
    assert {i["session_id"] for i in admin["queue"]} == {mine, theirs}
    assert [i["session_id"] for i in admin["active_tasks"]] == [running]
    assert admin["status"] == "running"
    _no_real_side_effect(synthetic)


def test_stop_changes_only_the_callers_synthetic_run(synthetic):
    mine, mine_item = _run(synthetic, QA1, "running")
    theirs, their_item = _run(synthetic, QA2, "running")

    denied = synthetic.client.post("/api/stop", json={"session_id": theirs}, headers=_as(QA1))
    stopped = synthetic.client.post("/api/stop", json={"session_id": mine}, headers=_as(QA1))

    assert denied.status_code == 403
    assert their_item["status"] == "running"
    assert stopped.json() == {"status": "stopped", "session_id": mine}
    assert mine_item["status"] == "stopped"
    _no_real_side_effect(synthetic)


def test_untargeted_stop_reaches_only_the_callers_one_run_and_no_device(synthetic):
    _, mine_item = _run(synthetic, QA1, "running")
    _, their_item = _run(synthetic, QA2, "running")

    by_device = synthetic.client.post(
        "/api/stop", json={"device_id": "emulator-5554"}, headers=_as(QA1)
    )
    untargeted = synthetic.client.post("/api/stop", headers=_as(QA1))
    both = synthetic.client.post(
        "/api/stop", json={"session_id": "s", "device_id": "d"}, headers=_as(QA1)
    )

    assert by_device.json() == {"status": "no_running_task"}
    assert untargeted.json()["status"] == "stopped"
    assert mine_item["status"] == "stopped"
    assert their_item["status"] == "running"
    assert both.status_code == 400
    _no_real_side_effect(synthetic)


def test_cancel_queued_needs_the_owner_and_only_cancels_a_waiting_run(synthetic):
    waiting, item = _run(synthetic, QA1, "pending")
    foreign, foreign_item = _run(synthetic, QA2, "pending")
    started, _ = _run(synthetic, QA1, "running")
    post = synthetic.client.post

    assert post(f"/api/tasks/{foreign}/cancel-queued", headers=_as(QA1)).status_code == 403
    assert foreign_item["status"] == "pending"
    assert post(f"/api/tasks/{started}/cancel-queued", headers=_as(QA1)).json()["status"] == (
        "already_started"
    )
    assert post(f"/api/tasks/{waiting}/cancel-queued", headers=_as(QA1)).json() == {
        "status": "cancelled",
        "session_id": waiting,
    }
    assert item["status"] == "cancelled"
    assert post(f"/api/tasks/{uuid.uuid4()}/cancel-queued", headers=_as(ADMIN)).status_code == 404
    _no_real_side_effect(synthetic)


def test_resume_clears_only_the_preview_pause_and_follows_the_ownership_rule(synthetic):
    _run(synthetic, QA2, "running")
    post = synthetic.client.post

    assert post("/api/resume", headers=_as(ADMIN)).json() == {"status": "not_paused"}
    preview_synthetic.control.paused = True
    assert post("/api/resume", headers=_as(QA1)).status_code == 403  # a foreign run is affected
    assert preview_synthetic.control.paused is True
    assert post("/api/resume", headers=_as(ADMIN)).json() == {"status": "resumed"}
    assert preview_synthetic.control.paused is False
    _no_real_side_effect(synthetic)


# -- the real preview app: every route, in a fresh process ---------------------

_PROBE = textwrap.dedent(
    """
    import json, os, re, sys
    import starlette.routing as sr

    handled, effects = [], []

    def record_handle(cls):
        original = cls.handle
        async def handle(self, scope, receive, send):
            handled.append(f"{self.endpoint.__module__.rsplit('.', 1)[-1]}.{self.endpoint.__name__}")
            await original(self, scope, receive, send)
        cls.handle = handle

    import fastapi.routing as fr
    for route_class in (sr.Route, sr.WebSocketRoute, fr.APIRoute, fr.APIWebSocketRoute):
        record_handle(route_class)

    def trap(owner, name, is_async=False):
        def hit(*_a, **_k):
            effects.append(name)
            raise AssertionError(name)
        async def ahit(*_a, **_k):
            hit()
        setattr(owner, name, ahit if is_async else hit)

    from artemis.core.diagnostics import readiness_engine
    from artemis.runtime import device_pool
    from artemis.runtime.device_lock import DeviceExecutionLock
    from apps.admin_console.services import run_retention
    from apps.admin_console.services.task_queue_service import task_queue_service
    trap(readiness_engine, "run_all", True)
    trap(device_pool, "list_devices_async", True)
    for name in ("stop_tasks", "resume_task", "cancel_queued", "ensure_worker_running"):
        trap(task_queue_service, name)
    for name in ("delete_run", "clear_all", "set_pinned", "enforce", "update_settings"):
        trap(run_retention, name)
    for name in ("get_active_owner", "get_active_owners", "get_queued_tasks"):
        setattr(DeviceExecutionLock, name, classmethod(lambda cls, *a, _n=name, **k: effects.append(_n)))

    import apps.admin_console.server as server
    from apps.admin_console.core import preview_routes as pr
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    @server.app.get("/api/rogue-added-later")
    async def rogue():
        return {}

    @server.app.head("/api/rogue-head")
    async def rogue_head():
        return {}

    headers = {"Cf-Access-Jwt-Assertion": os.environ["ARTEMIS_TEST_ACCESS_TOKEN"]}
    client = TestClient(server.app, base_url="http://localhost", raise_server_exceptions=False,
                        headers=headers)
    rooted = TestClient(
        server.app,
        base_url="http://localhost",
        root_path="/preview/pr-92",
        raise_server_exceptions=False,
        headers=headers,
    )
    keys = sorted(pr.ALLOWED | pr.DISABLED) + [
        "GET /api/rogue-added-later",
        "HEAD /api/rogue-head",
        "POST /api/never",
    ]
    skipped = {"GET /api/stream", "GET /api/stream/{session_id}"}  # endless SSE
    results = {}
    for key in keys:
        if key in skipped:
            continue
        method, path = key.split(" ", 1)
        url = re.sub(r"\\{[^}]+\\}", "x", path)
        handled.clear(); effects.clear()
        if method == "WS":
            try:
                with client.websocket_connect(url):
                    status, code = "accepted", None
            except WebSocketDisconnect as closed:
                status, code = closed.code, closed.reason
        else:
            response = client.request(method, url, json={} if method in ("POST", "PUT") else None)
            status = response.status_code
            try:
                body = response.json()
                code = body.get("code") if isinstance(body, dict) else None
            except ValueError:
                code = None
        results[key] = {"status": status, "code": code, "handled": list(handled),
                        "effects": list(effects)}
    for key in sorted(pr.DISABLED):
        method, path = key.split(" ", 1)
        if method != "GET":
            continue
        handled.clear(); effects.clear()
        response = rooted.get("/preview/pr-92" + re.sub(r"\\{[^}]+\\}", "x", path))
        results["ROOT " + key] = {"status": response.status_code,
                                  "code": (response.json() or {}).get("code")
                                  if response.headers.get("content-type", "").startswith("application/json")
                                  and isinstance(response.json(), dict) else None,
                                  "handled": list(handled), "effects": list(effects)}
    print(json.dumps({"preview": server.PREVIEW_PROFILE, "results": results}))
    """
)


@pytest.fixture(scope="module")
def preview_probe(tmp_path_factory, preview_access_env):
    tmp_path = tmp_path_factory.mktemp("preview-routes")
    env = {
        **os.environ,
        "ARTEMIS_APP_DIR": str(tmp_path / "app"),
        "TMPDIR": str(tmp_path),
        "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
        ENV_PREVIEW_PROFILE: "1",
        **preview_access_env,
    }
    try:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:  # an unguarded streaming route never returns
        pytest.fail("a route that must be refused reached its handler and hung")
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["preview"] is True
    return outcome["results"]


def test_preview_refuses_every_disabled_route_before_any_handler_or_effect(preview_probe):
    for key in pr.DISABLED:
        got = preview_probe[key]
        refused = got["code"] == pr.PREVIEW_DISABLED_CODE or got["status"] == 1008
        assert refused, (key, got)
        assert got["handled"] == [] and got["effects"] == [], (key, got)


def test_preview_refuses_every_disabled_get_route_behind_a_root_path(preview_probe):
    rooted = [key for key in preview_probe if key.startswith("ROOT ")]
    assert rooted
    for key in rooted:
        got = preview_probe[key]
        assert got["code"] == pr.PREVIEW_DISABLED_CODE, (key, got)
        assert got["handled"] == [] and got["effects"] == [], (key, got)


def test_preview_refuses_routes_nobody_classified(preview_probe):
    for key in ("GET /api/rogue-added-later", "HEAD /api/rogue-head", "POST /api/never"):
        got = preview_probe[key]
        # A HEAD response has no body to carry the code, so its 403 is the refusal.
        assert got["code"] == pr.PREVIEW_DISABLED_CODE or (
            key.startswith("HEAD") and got["status"] == 403
        ), (key, got)
        assert got["handled"] == [] and got["effects"] == [], (key, got)


def test_preview_answers_each_synthetic_route_from_the_synthetic_handler(preview_probe):
    for key in pr.SYNTHETIC:
        got = preview_probe[key]
        assert got["code"] != pr.PREVIEW_DISABLED_CODE, (key, got)
        assert got["status"] in (200, 404), (key, got)
        assert got["handled"] and all(h.startswith("preview_synthetic.") for h in got["handled"]), (
            key,
            got,
        )
        assert got["effects"] == [], (key, got)


def test_preview_lets_real_read_routes_through_to_their_real_handlers(preview_probe):
    for key in pr.REAL - {"GET /api/stream", "GET /api/stream/{session_id}"}:
        got = preview_probe[key]
        assert got["code"] != pr.PREVIEW_DISABLED_CODE, (key, got)
        assert got["handled"], (key, got)
        assert not any(h.startswith("preview_synthetic.") for h in got["handled"]), (key, got)
        assert got["effects"] == [], (key, got)
