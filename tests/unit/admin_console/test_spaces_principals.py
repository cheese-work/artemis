"""Stage 1 of the spaces contract (CHE-1385): principals, fail-closed actor, admin by subject."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

from fastapi import Request
from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import (
    AccessConfig,
    AdminAPIError,
    authenticate_request,
    config_from_environment,
)
from apps.admin_console.core.ownership import (
    SYSTEM_PRINCIPAL,
    OwnerScope,
    SystemPrincipal,
    owners_of,
    require_actor,
)
from apps.admin_console.database.repositories.principal_repository import (
    PrincipalRepository,
    PrincipalStoreNotReady,
)
from apps.admin_console.database.repositories.run_catalog_repository import (
    CatalogNotReady,
    run_catalog_repo,
)
from apps.admin_console.server import app
from artemis.data_engine.storage import StorageManager

ISSUER = "https://team.cloudflareaccess.com"


@pytest.fixture
def repo(tmp_path):
    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    return PrincipalRepository(db)


def _request(headers=None, client=("203.0.113.9", 51000)) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system/whoami",
            "client": client,
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
    )


def _verifier(claims):
    verifier = MagicMock()
    verifier.verify = AsyncMock(return_value=claims)
    return verifier


def _config(**changes) -> AccessConfig:
    base = {
        "auth_mode": "cloudflare",
        "audience": "aud",
        "issuer": ISSUER,
        "spaces_enabled": True,
        "admin_subjects": frozenset({"sub-admin"}),
    }
    return AccessConfig(**{**base, **changes})


async def _login(config, sub, email, repo_) -> object:
    from apps.admin_console.core import access_control

    access_control.principal_repo = repo_
    return await authenticate_request(
        _request({"Cf-Access-Jwt-Assertion": "t"}),
        config,
        _verifier({"sub": sub, "email": email}),
    )


@pytest.fixture(autouse=True)
def _restore_repo():
    from apps.admin_console.core import access_control

    original = access_control.principal_repo
    yield
    access_control.principal_repo = original


# -- principals -------------------------------------------------------------------


def test_first_login_creates_one_principal_and_claims_the_email(repo):
    first = repo.ensure_user(ISSUER, "sub-1", "qa@example.com")
    again = repo.ensure_user(ISSUER, "sub-1", "qa@example.com")

    assert first == again
    assert first.kind == "user"
    assert first.history_email == "qa@example.com"


def test_a_second_subject_with_the_same_email_never_claims_its_history(repo):
    holder = repo.ensure_user(ISSUER, "sub-1", "qa@example.com")
    other = repo.ensure_user(ISSUER, "sub-2", "qa@example.com")

    assert other.id != holder.id
    assert other.history_email is None
    assert repo.get(ISSUER, "sub-1").history_email == "qa@example.com"


def test_the_same_subject_under_another_issuer_is_another_principal(repo):
    one = repo.ensure_user(ISSUER, "sub-1", "qa@example.com")
    two = repo.ensure_user("https://other.cloudflareaccess.com", "sub-1", "qa@example.com")

    assert one.id != two.id


def test_an_email_change_updates_contact_data_but_keeps_the_key_and_claim(repo):
    before = repo.ensure_user(ISSUER, "sub-1", "old@example.com")
    after = repo.ensure_user(ISSUER, "sub-1", "new@example.com")

    assert after.id == before.id
    assert after.email == "new@example.com"
    assert after.history_email == "old@example.com"


def test_concurrent_first_logins_of_one_email_yield_one_claim(repo):
    async def go():
        return await asyncio.gather(
            *(
                asyncio.to_thread(repo.ensure_user, ISSUER, f"sub-{n}", "qa@example.com")
                for n in range(8)
            )
        )

    principals = asyncio.run(go())

    assert len({p.id for p in principals}) == 8
    assert sum(p.history_email is not None for p in principals) == 1


def _drop_principal_tables(repo):
    repo.get(ISSUER, "warm-up")  # get_db bootstraps the schema once per path, so do it first
    with sqlite3.connect(repo.db_path) as conn:
        for table in ("delegation_spaces", "delegations", "principals"):
            conn.execute(f"DROP TABLE {table}")


def test_a_missing_principal_table_is_not_ready(repo):
    _drop_principal_tables(repo)

    with pytest.raises(PrincipalStoreNotReady):
        repo.ensure_user(ISSUER, "sub-1", "qa@example.com")


# -- delegations ------------------------------------------------------------------


def test_a_delegation_covers_only_its_spaces_until_it_expires_or_is_revoked(repo):
    import time

    agent = repo.ensure_user(ISSUER, "agent", None)
    human = repo.ensure_user(ISSUER, "human", "h@example.com")
    other = repo.ensure_user(ISSUER, "other", "o@example.com")
    grant = repo.grant_delegation(agent.id, human.id, ["s1", "s2"], time.time() + 60)

    assert repo.delegation_covers(agent.id, human.id, "s1")
    assert repo.delegation_covers(agent.id, human.id, "s2")
    assert not repo.delegation_covers(agent.id, human.id, "s3")
    assert not repo.delegation_covers(agent.id, other.id, "s1")  # not on behalf of someone else

    assert repo.revoke_delegation(grant)
    assert not repo.delegation_covers(agent.id, human.id, "s1")

    repo.grant_delegation(agent.id, human.id, ["s1"], time.time() - 1)
    assert not repo.delegation_covers(agent.id, human.id, "s1")  # expired


# -- identity ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_keeps_the_verified_subject_and_provisions_the_principal(repo):
    identity = await _login(_config(), "sub-1", "QA@Example.com", repo)

    assert (identity.issuer, identity.subject, identity.email) == (
        ISSUER,
        "sub-1",
        "qa@example.com",
    )
    assert repo.get(ISSUER, "sub-1").email == "qa@example.com"


@pytest.mark.asyncio
async def test_a_token_without_a_subject_is_invalid(repo):
    identity = await _login(_config(), None, "qa@example.com", repo)

    assert identity.email is None
    assert identity.reason == "jwt_invalid"


@pytest.mark.asyncio
async def test_with_spaces_on_only_the_admin_subject_is_admin_not_the_email(repo):
    config = _config(admin_emails=frozenset({"qa@example.com"}))

    assert (await _login(config, "sub-admin", "other@example.com", repo)).admin is True
    assert (await _login(config, "sub-qa", "qa@example.com", repo)).admin is False


@pytest.mark.asyncio
async def test_with_spaces_off_the_legacy_email_list_still_admits_and_no_principal_is_written(repo):
    config = _config(spaces_enabled=False, admin_emails=frozenset({"qa@example.com"}))

    assert (await _login(config, "sub-qa", "qa@example.com", repo)).admin is True
    assert repo.get(ISSUER, "sub-qa") is None


@pytest.mark.asyncio
async def test_a_missing_principal_store_is_a_retryable_503(repo):
    _drop_principal_tables(repo)

    with pytest.raises(AdminAPIError) as refused:
        await _login(_config(), "sub-1", "qa@example.com", repo)

    assert refused.value.status_code == 503
    assert refused.value.code == "principal_store_not_ready"
    assert refused.value.retry_after


def test_there_is_no_default_admin(monkeypatch):
    for name in ("ARTEMIS_ADMIN_EMAILS", "ARTEMIS_ADMIN_SUBJECTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "aud")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "team")

    config = config_from_environment()

    assert config.admin_emails == frozenset()
    assert config.admin_subjects == frozenset()


def test_admin_subjects_keep_their_case(monkeypatch):
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "aud")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "team")
    monkeypatch.setenv("ARTEMIS_ADMIN_SUBJECTS", " AbC , def ")

    assert config_from_environment().admin_subjects == frozenset({"AbC", "def"})


# -- open mode --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "headers"),
    [(("203.0.113.9", 51000), {}), (("127.0.0.1", 51000), {"X-Forwarded-For": "203.0.113.9"})],
)
async def test_open_mode_with_spaces_refuses_anything_but_a_direct_loopback_caller(client, headers):
    config = AccessConfig(auth_mode="open", spaces_enabled=True)

    with pytest.raises(AdminAPIError) as refused:
        await authenticate_request(_request(headers, client), config, MagicMock())

    assert (refused.value.status_code, refused.value.code) == (403, "open_mode_loopback_only")


@pytest.mark.asyncio
async def test_open_mode_with_spaces_admits_a_direct_loopback_caller():
    config = AccessConfig(auth_mode="open", spaces_enabled=True)

    identity = await authenticate_request(
        _request(client=("127.0.0.1", 51000)), config, MagicMock()
    )

    assert identity.admin is True


@pytest.mark.asyncio
async def test_open_mode_without_spaces_is_unchanged_for_remote_callers():
    config = AccessConfig(auth_mode="open")

    identity = await authenticate_request(_request(), config, MagicMock())

    assert identity.admin is False


@pytest.mark.asyncio
async def test_a_remote_http_caller_in_open_mode_with_spaces_gets_403_over_the_wire(monkeypatch):
    monkeypatch.setattr(app.state, "access_config", AccessConfig("open", spaces_enabled=True))
    transport = ASGITransport(app=app, client=("203.0.113.9", 51000))

    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/api/system/whoami")

    assert response.status_code == 403
    assert response.json()["code"] == "open_mode_loopback_only"


# -- fail-closed actor ------------------------------------------------------------


@pytest.mark.parametrize("missing", [None, object(), "mine"])
def test_a_missing_actor_is_denied(missing):
    with pytest.raises(AdminAPIError) as refused:
        require_actor(missing)

    assert (refused.value.status_code, refused.value.code) == (401, "actor_required")


def test_a_real_scope_and_the_system_principal_pass_through():
    scope = OwnerScope(True, "qa@example.com")

    assert require_actor(scope) is scope
    assert require_actor(SYSTEM_PRINCIPAL) is SYSTEM_PRINCIPAL
    assert isinstance(SYSTEM_PRINCIPAL, SystemPrincipal)
    assert SYSTEM_PRINCIPAL.sees("anyone") and SYSTEM_PRINCIPAL.may_act_on(None)


# -- retryable 503 ----------------------------------------------------------------


def test_catalog_not_ready_through_ownership_is_a_retryable_503(monkeypatch):
    def not_ready(_ids):
        raise CatalogNotReady

    monkeypatch.setattr(run_catalog_repo, "owners", not_ready)

    with pytest.raises(AdminAPIError) as refused:
        owners_of(["run-1"])

    assert refused.value.status_code == 503
    assert refused.value.retry_after


@pytest.mark.asyncio
async def test_catalog_not_ready_on_the_run_routes_is_a_retryable_503(monkeypatch):
    def not_ready(*_args, **_kwargs):
        raise CatalogNotReady

    monkeypatch.setattr(run_catalog_repo, "list_runs", not_ready)
    monkeypatch.setattr(run_catalog_repo, "get_run", not_ready)
    monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))
    transport = ASGITransport(app=app, client=("127.0.0.1", 51000))

    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        listing = await client.get("/api/runs")
        one = await client.get("/api/runs/00000000-0000-4000-8000-000000000001")

    for response in (listing, one):
        assert response.status_code == 503
        assert response.headers["retry-after"]
        assert response.json()["retryable"] is True
