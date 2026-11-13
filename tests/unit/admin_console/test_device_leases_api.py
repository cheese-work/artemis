"""POST /api/leases and friends: flag, auth, busy answer, pools, restricted mode (CHE-2099)."""

import json

import pytest

from apps.admin_console.database.repositories.device_repository import DeviceRepository
from apps.admin_console.services import device_leases as leases

pytestmark = pytest.mark.asyncio


@pytest.fixture
def phones(library):
    repo = DeviceRepository(library.db)
    return [repo.create_device(owner_principal_id=None, label=f"phone-{n}") for n in range(2)]


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEVICE_LEASES", "enabled")
    monkeypatch.delenv("ARTEMIS_LEASE_POOLS", raising=False)
    monkeypatch.delenv("ARTEMIS_LEASE_RESTRICTED", raising=False)


async def lease(client, **body):
    return await client.post("/api/leases", json={"ttl": 60, "purpose": "qa", **body})


async def test_the_api_is_off_unless_the_flag_is_set(admin, phones, monkeypatch):
    monkeypatch.delenv("ARTEMIS_DEVICE_LEASES")

    async with admin:
        post = await lease(admin, device_id=phones[0].device_id)
        listing = await admin.get("/api/leases")

    assert post.status_code == listing.status_code == 404
    assert post.json()["code"] == "device_leases_disabled"


async def test_an_anonymous_caller_cannot_lease(anonymous, phones):
    async with anonymous:
        assert (await lease(anonymous, device_id=phones[0].device_id)).status_code == 401


async def test_a_qa_user_leases_a_phone_and_the_second_caller_gets_409_with_the_holder(
    qa, qa2, phones
):
    async with qa, qa2:
        first = await lease(qa, device_id=phones[0].device_id, purpose="first run")
        second = await lease(qa2, device_id=phones[0].device_id)

    assert first.status_code == 201
    assert first.json()["holder"] == "qa@example.com"
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "device_busy"
    assert body["holder"]["holder"] == "qa@example.com"
    assert body["holder"]["purpose"] == "first run"
    assert body["holder"]["expires_at"] == first.json()["expires_at"]
    assert "lease_id" not in body["holder"]


async def test_an_unknown_phone_is_404(qa):
    async with qa:
        got = await lease(qa, device_id="dev_nope")

    assert got.status_code == 404 and got.json()["code"] == "device_unknown"


async def test_an_alias_leases_the_canonical_phone(qa, qa2, phones, library):
    DeviceRepository(library.db).add_alias("dev_old_name", phones[0].device_id)

    async with qa, qa2:
        first = await lease(qa, device_id="dev_old_name")
        second = await lease(qa2, device_id=phones[0].device_id)

    assert first.json()["device_id"] == phones[0].device_id
    assert second.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"device_id": "a", "pool": "b"},
        {"device_id": "a", "ttl": 0},
        {"device_id": "a", "ttl": 99999},
    ],
)
async def test_a_malformed_request_is_422(qa, body):
    async with qa:
        got = await qa.post("/api/leases", json={"ttl": 60, **body})

    assert got.status_code == 422


async def test_renew_and_release_belong_to_the_holder_but_an_admin_may_do_both(
    qa, qa2, admin, phones
):
    async with qa, qa2, admin:
        made = (await lease(qa, device_id=phones[0].device_id)).json()
        url = f"/api/leases/{made['lease_id']}"

        other = await qa2.post(f"{url}/renew", json={"ttl": 300})
        mine = await qa.post(f"{url}/renew", json={"ttl": 300})
        by_admin = await admin.post(f"{url}/release")
        again = await qa.post(f"{url}/release")
        free = await lease(qa2, device_id=phones[0].device_id)

    assert other.status_code == 403 and other.json()["code"] == "lease_not_yours"
    assert mine.json()["expires_at"] > made["expires_at"]
    assert by_admin.status_code == 200
    assert again.status_code == 404
    assert free.status_code == 201


async def test_a_pool_leases_the_first_free_member(qa, qa2, phones, monkeypatch):
    monkeypatch.setenv(
        "ARTEMIS_LEASE_POOLS", json.dumps({"x99-avd": [p.device_id for p in phones]})
    )

    async with qa, qa2:
        first = await lease(qa, pool="x99-avd")
        second = await lease(qa2, pool="x99-avd")
        third = await lease(qa2, pool="x99-avd")
        missing = await lease(qa, pool="nope")

    assert [first.json()["device_id"], second.json()["device_id"]] == [p.device_id for p in phones]
    assert third.status_code == 409 and third.json()["code"] == "device_busy"
    assert missing.status_code == 404 and missing.json()["code"] == "pool_unknown"


async def test_the_restricted_mode_is_stored_and_a_configured_phone_is_always_restricted(
    qa, phones, monkeypatch
):
    monkeypatch.setenv("ARTEMIS_LEASE_RESTRICTED", phones[1].device_id)

    async with qa:
        plain = await lease(qa, device_id=phones[0].device_id)
        forced = await lease(qa, device_id=phones[1].device_id, restricted=False)

    assert plain.json()["restricted"] is False
    assert forced.json()["restricted"] is True


async def test_an_admin_lists_the_active_leases_and_a_qa_user_may_not(qa, admin, phones):
    async with qa, admin:
        await lease(qa, device_id=phones[0].device_id, restricted=True)
        listing = await admin.get("/api/leases")
        denied = await qa.get("/api/leases")

    [row] = listing.json()["leases"]
    assert row["device_id"] == phones[0].device_id and row["restricted"] is True
    assert denied.status_code == 403


async def test_an_expired_lease_frees_the_phone_over_the_api(qa, qa2, phones, monkeypatch):
    now = [5_000.0]
    monkeypatch.setattr(leases, "clock", lambda: now[0])

    async with qa, qa2:
        await lease(qa, device_id=phones[0].device_id, ttl=60)
        now[0] += 60
        got = await lease(qa2, device_id=phones[0].device_id)

    assert got.status_code == 201
