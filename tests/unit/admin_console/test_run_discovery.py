import sqlite3
from unittest.mock import AsyncMock

import pytest

from apps.admin_console.core.access_control import route_tier
from apps.admin_console.core.preview_routes import registered_routes, unclassified_routes
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import run_bundle as bundle_router
from apps.admin_console.routers import tasks
from apps.admin_console.server import app
from artemis.data_engine import run_catalog
from artemis.runtime.device_pool import DeviceStatus
from tests.unit.admin_console.test_run_ownership import ADMIN, QA1, QA2, _get, _post
from tests.unit.admin_console.test_run_ownership import cloudflare as cloudflare
from tests.unit.admin_console.test_run_ownership import env as env


def _seed(db, owner, suffix=1, prefix="abcd1234"):
    session_id = f"{prefix}-0000-4000-8000-{suffix:012d}"
    assert session_repo.create_queued_session(session_id, f"goal {suffix}", "flash", None, suffix)
    run_catalog_repo.set_meta(session_id, requested_by=owner)
    return session_id


def _ids(response):
    assert response.status_code == 200, response.text
    body = response.json()
    return {run["session_id"] for run in (body["runs"] if isinstance(body, dict) else body)}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/runs/{id}/bundle.zip"])
async def test_non_owner_needs_full_id_even_after_sharing(cloudflare, endpoint):
    session_id = _seed(cloudflare, QA1)
    path = endpoint + "/{id}" if endpoint == "/api/runs" else endpoint
    refused = await _get(QA2, path.format(id=session_id[:8]))
    assert refused.status_code == 404
    assert refused.json()["code"] == "run_not_visible"
    assert (await _get(QA2, path.format(id=session_id))).status_code == 200
    assert (await _get(QA2, path.format(id=session_id[:8]))).status_code == 404
    assert _ids(await _get(QA2, "/api/runs", scope="available")) == {session_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", [False, True])
async def test_prefix_candidates_are_owner_scoped_before_resolution(cloudflare, bundle):
    owned = _seed(cloudflare, QA1)
    foreign = _seed(cloudflare, QA2, 2)
    path = "/api/runs/abcd1234" + ("/bundle.zip" if bundle else "")
    response = await _get(QA1, path)
    assert response.status_code == 200
    if not bundle:
        assert response.json()["session_id"] == owned
    _seed(cloudflare, QA1, 3)
    ambiguous = await _get(QA1, path)
    assert ambiguous.status_code == 409
    assert foreign not in {run["session_id"] for run in ambiguous.json()["candidates"]}
    assert len((await _get(ADMIN, path)).json()["candidates"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle", [False, True])
async def test_foreign_tombstone_prefix_is_indistinguishable_from_missing(cloudflare, bundle):
    gone = _seed(cloudflare, QA1)
    run_catalog_repo.tombstone(gone, "admin_delete")
    suffix = "/bundle.zip" if bundle else ""
    hidden = await _get(QA2, f"/api/runs/{gone[:8]}{suffix}")
    missing = await _get(QA2, f"/api/runs/ffff1234{suffix}")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert (await _get(QA1, f"/api/runs/{gone[:8]}{suffix}")).status_code == 410
    assert (await _get(QA2, f"/api/runs/{gone}{suffix}")).status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/runs", "/api/sessions"])
async def test_available_is_own_plus_successfully_opened_full_ids(cloudflare, endpoint):
    owned = _seed(cloudflare, QA1)
    shared = _seed(cloudflare, QA2, 2)
    hidden = _seed(cloudflare, QA2, 3)
    unowned = _seed(cloudflare, None, 4)
    assert _ids(await _get(QA1, endpoint, scope="available")) == {owned}
    assert (await _get(QA1, f"/api/runs/{shared}")).status_code == 200
    assert _ids(await _get(QA1, endpoint, scope="available")) == {owned, shared}
    assert _ids(await _get(QA1, endpoint)) == {owned}
    assert _ids(await _get(QA2, endpoint, scope="available")) == {shared, hidden}
    assert (await _get(QA1, endpoint, scope="all")).status_code == 403
    assert _ids(await _get(ADMIN, endpoint, scope="all")) == {owned, shared, hidden, unowned}
    assert (await _post(QA1, f"/api/tasks/{shared}/cancel-queued")).status_code == 403


@pytest.mark.asyncio
async def test_available_filters_before_search_and_pagination(cloudflare):
    owned = _seed(cloudflare, QA1)
    shared = _seed(cloudflare, QA2, 2)
    _seed(cloudflare, QA2, 3)
    await _get(QA1, f"/api/runs/{shared}")
    first = await _get(QA1, "/api/runs", scope="available", limit=1, q="goal")
    assert _ids(first) == {shared}
    second = await _get(
        QA1, "/api/runs", scope="available", limit=1, q="goal", cursor=first.json()["next_cursor"]
    )
    assert _ids(second) == {owned}
    assert second.json()["next_cursor"] is None
    assert _ids(await _get(QA1, "/api/runs", scope="available", q="goal 3")) == set()


@pytest.mark.asyncio
async def test_failed_reads_do_not_record_link_shares(cloudflare):
    gone = _seed(cloudflare, QA2)
    run_catalog_repo.tombstone(gone, "retention")
    assert (await _get(QA1, f"/api/runs/{gone}")).status_code == 410
    assert _ids(await _get(QA1, "/api/runs", scope="available")) == set()
    with sqlite3.connect(cloudflare) as conn:
        assert conn.execute("SELECT count(*) FROM run_link_shares").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_failed_share_recording_releases_bundle_lease(cloudflare, monkeypatch):
    shared = _seed(cloudflare, QA2)

    def unavailable(*args):
        raise sqlite3.OperationalError("link-share storage unavailable")

    monkeypatch.setattr(bundle_router, "record_run_read", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="link-share storage unavailable"):
        await _get(QA1, f"/api/runs/{shared}/bundle.zip")
    with sqlite3.connect(cloudflare) as conn:
        assert conn.execute("SELECT count(*) FROM run_artifact_leases").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_link_shares_survive_migration_and_are_idempotent(cloudflare):
    shared = _seed(cloudflare, QA2)
    for _attempt in range(2):
        assert (await _get(QA1, f"/api/runs/{shared}")).status_code == 200
    run_catalog.migrate(cloudflare)
    with sqlite3.connect(cloudflare) as conn:
        assert conn.execute("SELECT email, session_id FROM run_link_shares").fetchall() == [
            (QA1, shared)
        ]
    assert _ids(await _get(QA1, "/api/runs", scope="available")) == {shared}
    run_catalog_repo.tombstone(shared, "retention")
    assert _ids(await _get(QA1, "/api/runs", scope="available")) == set()


def test_existing_catalog_adds_link_shares_without_rewriting_runs(env):
    owned = _seed(env, QA1)
    with sqlite3.connect(env) as conn:
        conn.execute("DROP TABLE run_link_shares")
    run_catalog.migrate(env)
    assert run_catalog_repo.get_run(owned).run["requested_by"] == QA1
    run_catalog_repo.record_link_share(QA2, owned)
    assert run_catalog_repo.shared_run_ids(QA2) == {owned}


@pytest.mark.asyncio
async def test_prefix_visibility_precedes_candidate_limit(cloudflare):
    owned = _seed(cloudflare, QA1)
    for suffix in range(2, 30):
        _seed(cloudflare, QA2, suffix)
    response = await _get(QA1, "/api/runs/abcd1234")
    assert response.status_code == 200 and response.json()["session_id"] == owned


@pytest.mark.asyncio
async def test_unowned_full_id_is_available_but_never_actionable(cloudflare):
    unowned = _seed(cloudflare, None)
    assert (await _get(QA1, f"/api/runs/{unowned[:8]}")).status_code == 404
    assert (await _get(QA1, f"/api/runs/{unowned}")).json()["read_only"] is True
    assert _ids(await _get(QA1, "/api/runs", scope="available")) == {unowned}
    assert (await _post(QA1, f"/api/tasks/{unowned}/cancel-queued")).status_code == 403


@pytest.mark.asyncio
async def test_open_mode_available_remains_unfiltered(env):
    owned = _seed(env, QA1)
    foreign = _seed(env, QA2, 2)
    assert _ids(await _get(None, "/api/runs", scope="available")) == {owned, foreign}
    assert (await _get(None, f"/api/runs/{owned[:8]}")).status_code == 409


@pytest.mark.asyncio
async def test_devices_response_preserves_client_contract(env, monkeypatch):
    monkeypatch.setattr(
        tasks.device_pool,
        "list_devices_async",
        AsyncMock(return_value=[DeviceStatus(serial="emulator-5554", state="device")]),
    )
    response = await _get(None, "/api/devices")
    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {
                "serial": "emulator-5554",
                "state": "device",
                "model": None,
                "product": None,
                "is_emulator": False,
                "device_kind": "unknown",
                "is_busy": False,
                "active_pid": None,
                "active_task_desc": None,
                "active_session_id": None,
                "acquired_at": None,
            }
        ]
    }


def test_every_registered_route_has_access_and_preview_classification():
    assert unclassified_routes(app) == []
    missing = [
        route.keys
        for route in registered_routes(app)
        if route_tier(route.path, set(route.methods) - {"WS"}, "WS" in route.methods) is None
    ]
    assert missing == []
