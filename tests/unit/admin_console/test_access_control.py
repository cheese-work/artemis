from __future__ import annotations

import json
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from starlette.requests import Request

import jwt
import pytest
from fastapi.testclient import TestClient

from apps.admin_console.core.access_control import (
    AccessConfig,
    AccessIdentity,
    _service_principals_from_environment,
    CloudflareAccessVerifier,
    authenticate_request,
    config_from_environment,
)


@pytest.fixture
def access_keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk["kid"] = "test-key"
    return private_pem, {"keys": [public_jwk]}


def request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system/whoami",
            "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        }
    )


def make_token(private_key: bytes, **claims) -> str:
    payload = {
        "iss": "https://team.cloudflareaccess.com",
        "aud": "app-audience",
        "sub": "user-1",
        "email": "admin@example.com",
        "iat": 1_790_000_000,
        "nbf": 1_790_000_000,
        "exp": 1_900_000_000,
        **claims,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.mark.asyncio
async def test_cloudflare_jwt_maps_admin_email_from_signed_claim(access_keys):
    private_key, jwks = access_keys
    config = AccessConfig(
        auth_mode="cloudflare",
        audience="app-audience",
        issuer="https://team.cloudflareaccess.com",
        admin_emails=frozenset({"admin@example.com"}),
    )
    verifier = CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks)
    token = make_token(private_key)

    identity = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}), config, verifier
    )

    assert identity.email == "admin@example.com"
    assert identity.admin is True
    assert identity.reason is None


@pytest.mark.asyncio
async def test_signed_non_admin_and_forged_email_header_remain_non_admin(access_keys):
    private_key, jwks = access_keys
    config = AccessConfig(
        auth_mode="cloudflare",
        audience="app-audience",
        issuer="https://team.cloudflareaccess.com",
        admin_emails=frozenset({"admin@example.com"}),
    )
    verifier = CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks)
    token = make_token(private_key, email="qa@example.com")

    signed_identity = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}), config, verifier
    )
    forged_identity = await authenticate_request(
        request_with_headers({"Cf-Access-Authenticated-User-Email": "admin@example.com"}),
        config,
        verifier,
    )

    assert signed_identity.email == "qa@example.com"
    assert signed_identity.admin is False
    assert signed_identity.reason == "not_on_allowlist"
    assert forged_identity.email is None
    assert forged_identity.admin is False
    assert forged_identity.reason == "no_jwt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "wrong-audience"},
        {"exp": 1},
    ],
)
async def test_invalid_claims_fail_closed(access_keys, claims):
    private_key, jwks = access_keys
    config = AccessConfig(
        auth_mode="cloudflare",
        audience="app-audience",
        issuer="https://team.cloudflareaccess.com",
        admin_emails=frozenset({"admin@example.com"}),
    )
    verifier = CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks)
    token = make_token(private_key, **claims)

    identity = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}), config, verifier
    )

    assert identity.admin is False
    assert identity.reason == "jwt_invalid"


@pytest.mark.asyncio
async def test_bad_signature_and_jwks_outage_fail_closed(access_keys):
    private_key, _jwks = access_keys
    config = AccessConfig(
        auth_mode="cloudflare",
        audience="app-audience",
        issuer="https://team.cloudflareaccess.com",
        admin_emails=frozenset({"admin@example.com"}),
    )
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    token = make_token(other_pem)
    verifier = CloudflareAccessVerifier(fetch_jwks=lambda _url: _jwks)

    invalid = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}), config, verifier
    )
    unavailable = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}),
        config,
        CloudflareAccessVerifier(fetch_jwks=lambda _url: (_ for _ in ()).throw(OSError())),
    )

    assert invalid.reason == "jwt_invalid"
    assert invalid.admin is False
    assert unavailable.reason == "jwks_unavailable"
    assert unavailable.admin is False


def test_cloudflare_configuration_normalizes_team_domain_and_allowlist(monkeypatch):
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "app-audience")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "team")
    monkeypatch.setenv("ARTEMIS_ADMIN_EMAILS", " Admin@Example.com,qa@example.com ")

    config = config_from_environment()

    assert config.issuer == "https://team.cloudflareaccess.com"
    assert config.admin_emails == frozenset({"admin@example.com", "qa@example.com"})


def test_cloudflare_configuration_fails_closed_when_required_values_are_missing(monkeypatch):
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.delenv("ARTEMIS_CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", raising=False)

    with pytest.raises(ValueError, match="ARTEMIS_CF_ACCESS_AUD"):
        config_from_environment()


@pytest.mark.asyncio
async def test_open_mode_does_not_trust_forwarded_loopback_peer():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/system/whoami",
            "headers": [(b"x-forwarded-for", b"198.51.100.10")],
            "client": ("127.0.0.1", 8000),
        }
    )

    identity = await authenticate_request(
        request,
        AccessConfig(auth_mode="open"),
        CloudflareAccessVerifier(fetch_jwks=lambda _url: {"keys": []}),
    )

    assert identity.admin is False
    assert identity.reason == "no_jwt"


def test_whoami_uses_signed_cloudflare_identity(monkeypatch, access_keys):
    from apps.admin_console.core.access_control import AccessConfig
    from apps.admin_console.server import app

    private_key, jwks = access_keys
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="app-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(
        app.state,
        "access_verifier",
        CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks),
    )

    response = TestClient(app, base_url="http://localhost").get(
        "/api/system/whoami",
        headers={"Cf-Access-Jwt-Assertion": make_token(private_key)},
    )

    assert response.status_code == 200
    assert response.json() == {
        "email": "admin@example.com",
        "admin": True,
        "auth_mode": "cloudflare",
        "reason": None,
    }


def test_denied_admin_action_has_no_side_effect(monkeypatch):
    from unittest.mock import AsyncMock

    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.server import app
    from apps.admin_console.routers import system

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="app-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())
    action = AsyncMock()
    monkeypatch.setattr(system.readiness_engine, "restart_adb_server", action)

    response = TestClient(app, base_url="http://localhost").post("/api/system/adb/restart")

    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"
    assert isinstance(response.json()["detail"], str)
    action.assert_not_awaited()


def test_cloudflare_admin_allowlist_defaults_to_cheese_and_normalizes(monkeypatch):
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "app-audience")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "team")
    monkeypatch.delenv("ARTEMIS_ADMIN_EMAILS", raising=False)

    assert config_from_environment().admin_emails == frozenset({"congvc.dev@gmail.com"})

    monkeypatch.setenv("ARTEMIS_ADMIN_EMAILS", " QA@example.test,Admin@example.test ")
    assert config_from_environment().admin_emails == frozenset(
        {"qa@example.test", "admin@example.test"}
    )


# -- service principals (Cloudflare Access service tokens) ----------------------------------

READER_CLIENT_ID = "reader-client.access"
READER_ENV = json.dumps(
    {READER_CLIENT_ID: {"name": "failures-reader", "routes": ["GET /api/system/failures"]}}
)


def service_token(private_key: bytes, client_id: str = READER_CLIENT_ID) -> str:
    """Cloudflare service-token JWT: no email, the client id in `common_name`."""
    payload = {
        "iss": "https://team.cloudflareaccess.com",
        "aud": "app-audience",
        "sub": "",
        "common_name": client_id,
        "iat": 1_790_000_000,
        "nbf": 1_790_000_000,
        "exp": 1_900_000_000,
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def service_app(monkeypatch, access_keys, library):
    from apps.admin_console.server import app

    private_key, jwks = access_keys
    monkeypatch.setenv("ARTEMIS_SERVICE_PRINCIPALS", READER_ENV)
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="app-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
            service_principals=_service_principals_from_environment(),
        ),
    )
    monkeypatch.setattr(
        app.state, "access_verifier", CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks)
    )
    client = TestClient(app, base_url="http://localhost")

    def call(method, path, client_id=READER_CLIENT_ID, **kwargs):
        headers = {"Cf-Access-Jwt-Assertion": service_token(private_key, client_id)}
        return client.request(method, path, headers=headers, **kwargs)

    return call


def test_mapped_failures_reader_gets_200_and_the_request_is_logged(service_app, caplog):
    caplog.set_level("INFO", logger="apps.admin_console.core.access_control")

    response = service_app("GET", "/api/system/failures?days=7")

    assert response.status_code == 200
    assert any(
        "principal=failures-reader" in line and "/api/system/failures" in line
        for line in caplog.messages
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/system/failures/collect"),
        ("POST", "/api/system/failures/digest"),
        ("GET", "/api/runs"),
        ("POST", "/api/run"),
        ("POST", "/api/runs/clear"),
        ("GET", "/api/system/config"),
        ("PUT", "/api/system/config"),
        ("GET", "/api/system/whoami"),
        ("GET", "/api/system/failures/extra"),
    ],
)
def test_failures_reader_is_denied_everywhere_else_and_the_denial_is_logged(
    service_app, caplog, monkeypatch, method, path
):
    from apps.admin_console.services import failure_ledger

    collect = Mock()
    monkeypatch.setattr(failure_ledger, "collect", collect)
    caplog.set_level("INFO", logger="apps.admin_console.core.access_control")

    response = service_app(method, path)

    assert response.status_code == 403
    collect.assert_not_called()
    assert any(
        "principal=failures-reader" in line and "allowed=False" in line for line in caplog.messages
    )


def test_unmapped_client_id_is_denied_even_on_the_failures_route(service_app):
    assert (
        service_app("GET", "/api/system/failures", client_id="stranger.access").status_code == 403
    )
    assert service_app("GET", "/api/runs", client_id="stranger.access").status_code == 403


@pytest.mark.asyncio
async def test_a_service_principal_never_satisfies_require_admin():
    from apps.admin_console.core.access_control import AdminAPIError, require_admin

    identity = AccessIdentity(None, False, "cloudflare", None, "failures-reader")

    with pytest.raises(AdminAPIError):
        await require_admin(identity)


@pytest.mark.asyncio
async def test_token_without_email_or_client_id_fails_closed(access_keys):
    private_key, jwks = access_keys
    config = AccessConfig(
        auth_mode="cloudflare",
        audience="app-audience",
        issuer="https://team.cloudflareaccess.com",
    )
    payload = {
        "iss": config.issuer,
        "aud": "app-audience",
        "sub": "",
        "iat": 1_790_000_000,
        "nbf": 1_790_000_000,
        "exp": 1_900_000_000,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "test-key"})

    identity = await authenticate_request(
        request_with_headers({"Cf-Access-Jwt-Assertion": token}),
        config,
        CloudflareAccessVerifier(fetch_jwks=lambda _url: jwks),
    )

    assert identity.principal is None
    assert identity.reason == "jwt_invalid"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"c": {"name": "x"}}',
        '{"c": {"name": "x", "routes": []}}',
        '{"c": {"name": "x", "routes": ["/api/system/failures"]}}',
        '{"c": {"routes": ["GET /api/system/failures"]}}',
    ],
)
def test_malformed_service_principal_mapping_fails_startup(monkeypatch, raw):
    monkeypatch.setenv("ARTEMIS_AUTH_MODE", "cloudflare")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_AUD", "app-audience")
    monkeypatch.setenv("ARTEMIS_CF_ACCESS_TEAM_DOMAIN", "team")
    monkeypatch.setenv("ARTEMIS_SERVICE_PRINCIPALS", raw)

    with pytest.raises(ValueError, match="ARTEMIS_SERVICE_PRINCIPALS"):
        config_from_environment()
