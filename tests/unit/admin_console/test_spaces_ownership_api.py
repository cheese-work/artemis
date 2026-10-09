# ruff: noqa: F811
"""Stage 1 of the spaces contract at the API seam (CHE-1385): ownership, admin, readiness."""

import asyncio
from pathlib import Path
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core import access_control
from apps.admin_console.core.access_control import AccessConfig
from apps.admin_console.core.ownership import OwnerScope
from apps.admin_console.database.repositories.principal_repository import PrincipalRepository
from apps.admin_console.database.repositories.run_catalog_repository import (
    CatalogNotReady,
    run_catalog_repo,
)
from apps.admin_console.server import app

# The run-ownership harness: one isolated database, no real device locks or workers.
from test_run_ownership import _run, env  # noqa: F401  (env is a fixture)

ISSUER = "https://team.cloudflareaccess.com"
ADMIN_SUB = "sub-admin"
ADMIN_EMAIL = "admin@example.com"


@pytest.fixture
def spaces(env, monkeypatch):  # noqa: F811
    """Cloudflare mode with spaces on. A token reads ``email|sub``."""
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="aud",
            issuer=ISSUER,
            spaces_enabled=True,
            admin_subjects=frozenset({ADMIN_SUB}),
            admin_emails=frozenset({ADMIN_EMAIL}),  # ignored while spaces are on
        ),
    )

    def claims(token, _config):
        email, sub = token.split("|")
        return {"email": email, "sub": sub}

    verifier = MagicMock()
    verifier.verify = AsyncMock(side_effect=claims)
    monkeypatch.setattr(app.state, "access_verifier", verifier)
    monkeypatch.setattr(access_control, "principal_repo", PrincipalRepository(env))
    return env


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
        base_url="http://localhost",
    )


async def _call(method: str, who: str | None, path: str, **params):
    headers = {"Cf-Access-Jwt-Assertion": who} if who else {}
    async with _client() as client:
        return await client.request(method, path, params=params, headers=headers)


def _ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["session_id"] for row in response.json()["runs"]}


A = "qa@example.com|sub-a"
B = "qa@example.com|sub-b"
OLD = "old@example.com|sub-a"
NEW = "new@example.com|sub-a"
B_NEW = "new@example.com|sub-b"
ADMIN = f"{ADMIN_EMAIL}|{ADMIN_SUB}"


@pytest.mark.asyncio
async def test_the_first_principal_to_present_an_email_owns_its_legacy_runs(spaces):
    run = _run(spaces, "qa@example.com")

    assert _ids(await _call("GET", A, "/api/runs")) == {run}


@pytest.mark.asyncio
async def test_a_second_subject_with_the_same_email_contests_it_and_neither_owns_the_runs(spaces):
    run = _run(spaces, "qa@example.com")
    assert _ids(await _call("GET", A, "/api/runs")) == {run}

    assert _ids(await _call("GET", B, "/api/runs")) == set()  # B reserved nothing
    assert _ids(await _call("GET", A, "/api/runs")) == set()  # the address is contested now


@pytest.mark.asyncio
async def test_a_contested_owner_cannot_act_on_the_legacy_run(spaces):
    run = _run(spaces, "qa@example.com")
    await _call("GET", A, "/api/runs")
    await _call("GET", B, "/api/runs")

    for who in (A, B):
        response = await _call("POST", who, f"/api/sessions/{run}/delete")
        assert response.status_code == 403
        assert response.json()["code"] == "not_run_owner"


@pytest.mark.asyncio
async def test_email_change_then_a_new_principal_on_the_new_address_never_takes_the_history(spaces):
    old_run = _run(spaces, "old@example.com")
    new_run = _run(spaces, "new@example.com")
    assert _ids(await _call("GET", OLD, "/api/runs")) == {old_run}

    assert _ids(await _call("GET", NEW, "/api/runs")) == {old_run, new_run}  # A moved to new@
    assert _ids(await _call("GET", B_NEW, "/api/runs")) == set()  # B reserves nothing
    assert _ids(await _call("GET", NEW, "/api/runs")) == {old_run}  # new@ is contested; old@ stays


@pytest.mark.asyncio
async def test_a_global_admin_without_a_grant_cannot_list_or_act_on_unrelated_runs(spaces):
    run = _run(spaces, "qa@example.com")
    await _call("GET", A, "/api/runs")

    assert _ids(await _call("GET", ADMIN, "/api/runs")) == set()
    listing_all = await _call("GET", ADMIN, "/api/runs", scope="all")
    assert listing_all.status_code == 403
    assert listing_all.json()["code"] == "scope_all_requires_admin"
    deleted = await _call("POST", ADMIN, f"/api/sessions/{run}/delete")
    assert deleted.status_code == 403
    assert deleted.json()["code"] == "not_run_owner"


@pytest.mark.asyncio
async def test_the_global_admin_keeps_platform_routes(spaces):
    whoami = await _call("GET", ADMIN, "/api/system/whoami")

    assert whoami.json()["admin"] is True
    assert whoami.json()["subject"] == ADMIN_SUB


@pytest.mark.asyncio
async def test_with_spaces_on_the_legacy_admin_email_is_not_an_admin(spaces):
    impostor = f"{ADMIN_EMAIL}|sub-not-admin"

    assert (await _call("GET", impostor, "/api/system/whoami")).json()["admin"] is False


@pytest.mark.asyncio
async def test_scope_everyone_is_refused_while_spaces_are_on(spaces):
    refused = await _call("GET", A, "/api/runs", scope="everyone")

    assert refused.status_code == 400
    assert refused.json()["code"] == "invalid_scope"


# -- retryable 503 on every ownership-checked route ----------------------------------

_OWNER_CHECKED = [
    ("GET", "/api/sessions/{id}"),
    ("GET", "/api/sessions/{id}/events"),
    ("GET", "/api/sessions/{id}/tree"),
    ("GET", "/api/sessions/{id}/background_tasks"),
    ("GET", "/api/sessions/{id}/startup_progress"),
    ("GET", "/api/sessions/{id}/goal-images/0"),
    ("GET", "/api/sessions/{id}/notes"),
    ("GET", "/api/sessions/{id}/video"),
    ("GET", "/api/sessions/{id}/replay_steps"),
    ("POST", "/api/sessions/{id}/delete"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _OWNER_CHECKED)
async def test_a_catalog_that_is_not_ready_answers_503_never_data(
    spaces, monkeypatch, method, path
):
    run = _run(spaces, "qa@example.com")

    def not_ready(_ids):
        raise CatalogNotReady

    monkeypatch.setattr(run_catalog_repo, "owners", not_ready)
    # Evidence routes check run visibility (live_run_ids), not ownership (CHE-1372).
    monkeypatch.setattr(run_catalog_repo, "live_run_ids", not_ready)

    response = await _call(method, A, path.format(id=run))

    assert response.status_code == 503, (method, path, response.status_code, response.text[:200])
    assert response.headers["retry-after"]
    assert response.json()["retryable"] is True


def test_the_task_plan_service_does_not_turn_a_not_ready_catalog_into_text(
    spaces, monkeypatch, tmp_path
):
    from apps.admin_console.core.access_control import AdminAPIError
    from apps.admin_console.core.ownership import OwnerScope
    from apps.admin_console.services import media_service

    run = _run(spaces, "qa@example.com")
    plan = tmp_path / "traces" / run / "notes"
    plan.mkdir(parents=True)
    (plan / "task_plan.md").write_text("step 1", encoding="utf-8")
    monkeypatch.setitem(
        media_service.MediaService.get_task_plan_content.__globals__,
        "TRACES_PATH",
        tmp_path / "traces",
    )

    def not_ready(_ids):
        raise CatalogNotReady

    monkeypatch.setattr(run_catalog_repo, "owners", not_ready)

    with pytest.raises(AdminAPIError) as refused:
        media_service.media_service.get_task_plan_content(run, OwnerScope(True, "qa@example.com"))

    assert refused.value.status_code == 503


# -- SystemPrincipal stays internal ---------------------------------------------------


def test_no_router_module_references_the_system_principal():
    routers = Path(__file__).resolve().parents[3] / "apps" / "admin_console" / "routers"
    offenders = [
        path.name
        for path in routers.glob("*.py")
        if re.search(r"SYSTEM_PRINCIPAL|SystemPrincipal", path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


# -- readiness is checked before any unscoped return ----------------------------------


def _catalog_down(monkeypatch):
    from artemis.data_engine import run_catalog

    monkeypatch.setattr(run_catalog, "catalog_ready", lambda _conn: False)


def test_the_system_principal_keeps_its_authority_when_the_catalog_is_ready(spaces):
    from apps.admin_console.core.ownership import (
        SYSTEM_PRINCIPAL,
        present_session_data,
        require_access_all,
    )

    run = _run(spaces, "qa@example.com")

    assert present_session_data(SYSTEM_PRINCIPAL, run, {"k": "v"}) == {"k": "v"}
    require_access_all(SYSTEM_PRINCIPAL, {run})  # does not raise


def test_the_system_principal_never_returns_raw_data_while_the_catalog_is_unready(
    spaces, monkeypatch
):
    from apps.admin_console.core.access_control import AdminAPIError
    from apps.admin_console.core.ownership import (
        SYSTEM_PRINCIPAL,
        present_session_data,
        require_access_all,
    )

    run = _run(spaces, "qa@example.com")
    _catalog_down(monkeypatch)

    with pytest.raises(AdminAPIError) as shown:
        present_session_data(SYSTEM_PRINCIPAL, run, {"k": "v"})
    with pytest.raises(AdminAPIError) as acted:
        require_access_all(SYSTEM_PRINCIPAL, {run})

    for refused in (shown, acted):
        assert refused.value.status_code == 503
        assert refused.value.retry_after


@pytest.mark.asyncio
async def test_a_contested_principal_gets_503_not_an_empty_list_while_the_catalog_is_unready(
    spaces, monkeypatch
):
    _run(spaces, "qa@example.com")
    await _call("GET", A, "/api/runs")
    await _call("GET", B, "/api/runs")  # B contests the address: A and B own nothing now

    ready = await _call("GET", B, "/api/runs")
    assert ready.status_code == 200
    assert ready.json()["runs"] == []

    _catalog_down(monkeypatch)
    for who in (A, B):
        down = await _call("GET", who, "/api/runs")
        assert down.status_code == 503, down.text
        assert down.headers["retry-after"]
        assert down.json()["retryable"] is True


@pytest.mark.asyncio
async def test_an_anonymous_empty_owner_list_also_waits_for_the_catalog(spaces, monkeypatch):
    _catalog_down(monkeypatch)

    down = await _call("GET", None, "/api/runs")

    assert down.status_code in (401, 403, 503)  # the access tier may refuse first


# -- readiness on status, session listings and streams, in every mode ---------------


def _open_mode(monkeypatch):
    monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))


async def _local(path: str):
    transport = ASGITransport(app=app, client=("127.0.0.1", 51000))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/sessions"])
async def test_open_mode_answers_503_not_data_while_the_catalog_is_unready(env, monkeypatch, path):
    _open_mode(monkeypatch)
    ready = await _local(path)
    assert ready.status_code == 200, ready.text[:200]

    _catalog_down(monkeypatch)
    down = await _local(path)

    assert down.status_code == 503, (path, down.status_code, down.text[:200])
    assert down.headers["retry-after"]
    assert down.json()["retryable"] is True


@pytest.mark.asyncio
async def test_an_unscoped_event_stream_does_not_open_while_the_catalog_is_unready(
    env, monkeypatch
):
    _open_mode(monkeypatch)
    _catalog_down(monkeypatch)

    # A stream that opens never ends, so a missing refusal shows up as a timeout.
    down = await asyncio.wait_for(_local("/api/stream"), timeout=10)

    assert down.status_code == 503
    assert down.headers["retry-after"]


@pytest.mark.asyncio
async def test_a_signed_in_event_stream_does_not_open_while_the_catalog_is_unready(
    spaces, monkeypatch
):
    # scope=mine with no active run never looks up an owner, so only the open-time check can refuse.
    _catalog_down(monkeypatch)

    down = await asyncio.wait_for(_call("GET", A, "/api/stream", scope="mine"), timeout=10)

    assert down.status_code == 503
    assert down.headers["retry-after"]
    assert down.json()["retryable"] is True


@pytest.mark.asyncio
async def test_a_signed_in_event_stream_opens_when_the_catalog_is_ready(spaces):
    from apps.admin_console.routers import tasks

    scope = OwnerScope(True, "qa@example.com", owned_emails=frozenset({"qa@example.com"}))

    response = await tasks.stream_events("all", None, scope)

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    await response.body_iterator.aclose()  # the stream never ends by itself


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/sessions"])
async def test_a_signed_in_caller_with_nothing_to_list_still_gets_503_while_the_catalog_is_unready(
    spaces, monkeypatch, path
):
    # No runs and an empty queue: the owner lookup has no ids, which used to skip the catalog.
    ready = await _call("GET", A, path)
    assert ready.status_code == 200, ready.text[:200]

    _catalog_down(monkeypatch)
    down = await _call("GET", A, path)

    assert down.status_code == 503, (path, down.status_code, down.text[:200])
    assert down.headers["retry-after"]


@pytest.mark.asyncio
async def test_an_admin_scope_all_listing_waits_for_the_catalog_outside_spaces(env, monkeypatch):
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="aud",
            issuer=ISSUER,
            admin_emails=frozenset({ADMIN_EMAIL}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(return_value={"email": ADMIN_EMAIL, "sub": ADMIN_SUB})
    monkeypatch.setattr(app.state, "access_verifier", verifier)
    _catalog_down(monkeypatch)

    down = await _call("GET", "jwt", "/api/status", scope="all")

    assert down.status_code == 503


# -- delegation need is a closed set --------------------------------------------------


@pytest.mark.parametrize("need", ["delete", "", "READ", "admin", None, 1])
def test_an_unsupported_delegation_need_is_rejected_before_any_authorization(spaces, need):
    import time

    repo = PrincipalRepository(spaces)
    agent = repo.ensure_user(ISSUER, "agent", None)
    human = repo.ensure_user(ISSUER, "human", "h@example.com")
    repo.grant_delegation(agent.id, human.id, ["s1"], time.time() + 60, mode="read")

    with pytest.raises(ValueError, match="unsupported delegation need"):
        repo.delegation_covers(agent.id, human.id, "s1", need=need)


# -- run-library delete and scope=all (Luna-2 verdict on ea1182a) -----------------------


def _alive(db, run_id: str) -> bool:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT deleted_at FROM run_meta WHERE session_id = ?", (run_id,)
        ).fetchone()
    return row is not None and row[0] is None


@pytest.mark.asyncio
async def test_a_global_admin_cannot_delete_another_persons_run_through_the_library_route(spaces):
    run = _run(spaces, "qa@example.com")
    await _call("GET", A, "/api/runs")

    refused = await _call("POST", ADMIN, f"/api/runs/{run}/delete")

    assert refused.status_code == 403
    assert refused.json()["code"] == "not_run_owner"
    assert _alive(spaces, run)


@pytest.mark.asyncio
async def test_a_global_admin_can_delete_their_own_run_through_the_library_route(spaces):
    mine = _run(spaces, ADMIN_EMAIL)
    await _call("GET", ADMIN, "/api/runs")  # reserves the admin's address

    deleted = await _call("POST", ADMIN, f"/api/runs/{mine}/delete")

    assert deleted.status_code == 200, deleted.text
    assert not _alive(spaces, mine)


@pytest.mark.asyncio
async def test_a_signed_in_non_admin_still_cannot_use_the_admin_delete_route(spaces):
    run = _run(spaces, "qa@example.com")
    await _call("GET", A, "/api/runs")

    refused = await _call("POST", A, f"/api/runs/{run}/delete")

    assert refused.status_code == 403
    assert refused.json()["code"] == "admin_required"
    assert _alive(spaces, run)


@pytest.mark.asyncio
async def test_outside_spaces_an_admin_still_deletes_any_run_through_the_library_route(
    env, monkeypatch
):
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="aud",
            issuer=ISSUER,
            admin_emails=frozenset({ADMIN_EMAIL}),
        ),
    )
    verifier = MagicMock()
    verifier.verify = AsyncMock(return_value={"email": ADMIN_EMAIL, "sub": ADMIN_SUB})
    monkeypatch.setattr(app.state, "access_verifier", verifier)
    run = _run(env, "qa@example.com")

    deleted = await _call("POST", "jwt", f"/api/runs/{run}/delete")

    assert deleted.status_code == 200, deleted.text
    assert not _alive(env, run)


@pytest.mark.asyncio
async def test_the_system_wide_clear_stays_an_admin_operation_under_spaces(spaces):
    async with _client() as client:
        response = await client.post(
            "/api/runs/clear",
            json={"confirm_count": 999},
            headers={"Cf-Access-Jwt-Assertion": ADMIN},
        )

    assert response.status_code != 403  # reaches the typed-count check, not an ownership refusal


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/status", "/api/sessions", "/api/runs"])
async def test_open_mode_with_spaces_rejects_scope_all_for_a_direct_loopback_caller(
    env, monkeypatch, path
):
    monkeypatch.setattr(
        app.state, "access_config", AccessConfig(auth_mode="open", spaces_enabled=True)
    )

    allowed = await _local(path)
    refused = await _local(f"{path}?scope=all")

    assert allowed.status_code == 200, allowed.text[:200]
    assert refused.status_code == 403, refused.text[:200]
    assert refused.json()["code"] == "scope_all_requires_admin"


@pytest.mark.asyncio
async def test_open_mode_without_spaces_keeps_scope_all(env, monkeypatch):
    _open_mode(monkeypatch)

    assert (await _local("/api/sessions?scope=all")).status_code == 200
