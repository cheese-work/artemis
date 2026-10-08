"""Stage 1 of the spaces contract at the API seam (CHE-1385): ownership, admin, readiness."""

from pathlib import Path
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core import access_control
from apps.admin_console.core.access_control import AccessConfig
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
