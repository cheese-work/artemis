"""A service principal runs the whole lease lifecycle through the real verifier and middleware (CHE-2099).

The API tests override ``public_tier`` with human identities, so they cannot see the exact-path
check in ``ServicePrincipalMiddleware``. A lease id is random: a grant must be able to name the
renew and release routes as templates.
"""

import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
import jwt
import pytest

from apps.admin_console.core.access_control import (
    AccessConfig,
    AdminAPIError,
    CloudflareAccessVerifier,
    ServicePrincipal,
    ServicePrincipalMiddleware,
    admin_api_error_handler,
)
from apps.admin_console.database.repositories.device_repository import DeviceRepository
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.routers.leases import router
from artemis.data_engine.storage import StorageManager

ISSUER = "https://review.cloudflareaccess.com"
RENEW = ("POST", "/api/leases/{lease_id}/renew")
RELEASE = ("POST", "/api/leases/{lease_id}/release")
ACQUIRE = ("POST", "/api/leases")


@pytest.fixture
def phone(tmp_path, monkeypatch):
    database = tmp_path / "data_engine.db"
    StorageManager(database, tmp_path)
    monkeypatch.setenv("ARTEMIS_DEVICE_LEASES", "enabled")
    monkeypatch.setattr(run_catalog_repo, "db_path", database)
    return DeviceRepository(database).create_device(owner_principal_id=None, label="service-phone")


@pytest.fixture
def key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_key.update(kid="k", alg="RS256", use="sig")
    return private_key, public_key


def service_client(key, **grants):
    """A client per principal: ``grants`` maps a client id to (name, routes)."""
    private_key, public_key = key
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(AdminAPIError, admin_api_error_handler)
    app.add_middleware(ServicePrincipalMiddleware)
    app.state.access_config = AccessConfig(
        auth_mode="cloudflare",
        audience="aud",
        issuer=ISSUER,
        service_principals={
            client_id: ServicePrincipal(name, frozenset(routes))
            for client_id, (name, routes) in grants.items()
        },
    )
    app.state.access_verifier = CloudflareAccessVerifier(
        fetch_jwks=lambda _url: {"keys": [public_key]}
    )

    def client(client_id):
        token = jwt.encode(
            {
                "type": "app",
                "iss": ISSUER,
                "aud": ["aud"],
                "sub": "",
                "common_name": client_id,
                "iat": int(time.time()) - 1,
                "exp": int(time.time()) + 3600,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "k"},
        )
        return TestClient(app, headers={"Cf-Access-Jwt-Assertion": token})

    return client


def test_a_service_agent_acquires_renews_and_releases_with_template_grants(key, phone):
    client = service_client(key, agent=("qa-agent", {ACQUIRE, RENEW, RELEASE}))("agent")

    created = client.post("/api/leases", json={"device_id": phone.device_id, "ttl": 60})
    path = f"/api/leases/{created.json()['lease_id']}"
    renewed = client.post(f"{path}/renew", json={"ttl": 60})
    released = client.post(f"{path}/release")

    assert (created.status_code, renewed.status_code, released.status_code) == (201, 200, 200)
    assert created.json()["holder"] == "qa-agent"


def test_a_grant_without_the_template_still_denies_the_lease_routes(key, phone):
    client = service_client(key, agent=("qa-agent", {ACQUIRE}))("agent")
    created = client.post("/api/leases", json={"device_id": phone.device_id, "ttl": 60})

    renewed = client.post(f"/api/leases/{created.json()['lease_id']}/renew", json={"ttl": 60})

    assert renewed.status_code == 403
    assert renewed.json()["code"] == "service_route_denied"


def test_a_template_grants_only_its_own_method_suffix_and_one_segment(key, phone):
    client = service_client(key, agent=("qa-agent", {ACQUIRE, RENEW}))("agent")
    created = client.post("/api/leases", json={"device_id": phone.device_id, "ttl": 60})
    lease_id = created.json()["lease_id"]

    release = client.post(f"/api/leases/{lease_id}/release")
    nested = client.post(f"/api/leases/{lease_id}/extra/renew", json={"ttl": 60})
    empty = client.post("/api/leases//renew", json={"ttl": 60})

    assert {r.status_code for r in (release, nested, empty)} == {403}
    assert release.json()["code"] == "service_route_denied"


def test_a_template_does_not_open_unrelated_service_routes(key, phone):
    client = service_client(key, agent=("qa-agent", {ACQUIRE, RENEW, RELEASE}))("agent")

    other = client.get("/api/leases")

    assert other.status_code == 403
    assert other.json()["code"] == "service_route_denied"


def test_a_service_agent_cannot_renew_another_agents_lease(key, phone):
    clients = service_client(
        key,
        one=("agent-one", {ACQUIRE, RENEW, RELEASE}),
        two=("agent-two", {ACQUIRE, RENEW, RELEASE}),
    )
    one, two = clients("one"), clients("two")
    created = one.post("/api/leases", json={"device_id": phone.device_id, "ttl": 60})

    renewed = two.post(f"/api/leases/{created.json()['lease_id']}/renew", json={"ttl": 60})

    assert renewed.status_code == 403
    assert renewed.json()["code"] == "lease_not_yours"
