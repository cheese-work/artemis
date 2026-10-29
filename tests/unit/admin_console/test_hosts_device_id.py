"""``/api/hosts`` names each phone by its canonical device id (CHE-2100, S3.3).

The Phones page leases by ``device_id``; a computer id plus serial resolves to nothing.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import AccessConfig
from apps.admin_console.database.repositories.device_repository import (
    DeviceRepository,
    DeviceStoreNotReady,
)
from apps.admin_console.routers import hosts as hosts_router
from apps.admin_console.server import app
from apps.admin_console.services.bridge_session_service import (
    BridgeSession,
    bridge_session_service,
)
from apps.admin_console.services.host_registry import host_registry

BROWSER_PHONE = "127.0.0.1:41001"

HOST_PHONE = {
    "serial": "sd-05b42e59020251f2",
    "model": "Pixel 7",
    "device_kind": "phone",
    "source": "computer",
    "computer_id": "h1",
    "computer_name": "Lab computer",
    "computer_status": "online",
    "reason": None,
    "since": None,
}


class Matched:
    """Routing keys the identity layer has already matched; stands in for the device store."""

    known: dict = {}
    not_ready = False

    def connection(self, *, source, host_id=None, serial):
        if Matched.not_ready:
            raise DeviceStoreNotReady
        device_id = Matched.known.get((source, host_id, serial))
        return SimpleNamespace(device_id=device_id) if device_id else None


async def _hosts() -> dict:
    transport = ASGITransport(app=app, client=("203.0.113.9", 51000))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/api/hosts")
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def hosts(monkeypatch):
    monkeypatch.setattr(app.state, "access_config", AccessConfig(auth_mode="open"))
    session = BridgeSession(
        session_id="s1", port=41001, created_at=0.0, expires_at=float("inf"), owner=None
    )
    monkeypatch.setattr(bridge_session_service, "_sessions", {"s1": session})
    monkeypatch.setattr(
        bridge_session_service,
        "live_sessions",
        lambda: list(bridge_session_service._sessions.values()),
    )
    monkeypatch.setattr(
        hosts_router.device_pool,
        "pool_for",
        lambda _endpoint: SimpleNamespace(list_devices_async=AsyncMock(return_value=[])),
    )
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "enabled")
    monkeypatch.setattr(host_registry, "list_hosts", lambda: ([], [dict(HOST_PHONE)]))
    monkeypatch.setattr(DeviceRepository, "connection", Matched.connection)
    monkeypatch.setattr(Matched, "known", {("host", "h1", HOST_PHONE["serial"]): "dev_abc"})
    monkeypatch.setattr(Matched, "not_ready", False)


@pytest.mark.asyncio
async def test_a_computers_phone_carries_its_canonical_device_id(hosts):
    body = await _hosts()
    phone = next(d for d in body["devices"] if d["serial"] == HOST_PHONE["serial"])
    assert phone["device_id"] == "dev_abc"


@pytest.mark.asyncio
async def test_a_phone_the_identity_layer_has_not_matched_has_no_device_id(hosts):
    body = await _hosts()
    mine = next(d for d in body["devices"] if d["serial"] == BROWSER_PHONE)
    assert mine["device_id"] is None


@pytest.mark.asyncio
async def test_a_missing_device_store_still_lists_phones(hosts, monkeypatch):
    monkeypatch.setattr(Matched, "not_ready", True)
    body = await _hosts()
    assert body["devices"] and all(d["device_id"] is None for d in body["devices"])


# --- CHE-2377: every holder's lease shows on the phone, not only the caller's own ---


def _lease(holder="other@example.com", purpose="nightly run", ttl=600):
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
    from apps.admin_console.services import device_leases

    return device_leases.acquire(run_catalog_repo.db_path, ["dev_abc"], holder, ttl, purpose)


@pytest.fixture
def leasing(hosts, library, monkeypatch):
    monkeypatch.setenv("ARTEMIS_DEVICE_LEASES", "enabled")


@pytest.mark.asyncio
async def test_a_phone_leased_by_someone_else_shows_holder_purpose_and_expiry(leasing):
    lease = _lease()
    body = await _hosts()
    phone = next(d for d in body["devices"] if d["serial"] == HOST_PHONE["serial"])
    assert phone["lease"] == {
        "holder": "other@example.com",
        "purpose": "nightly run",
        "expires_at": lease.expires_at,
    }


@pytest.mark.asyncio
async def test_a_free_phone_has_no_lease(leasing):
    body = await _hosts()
    assert all(d["lease"] is None for d in body["devices"])


@pytest.mark.asyncio
async def test_leases_stay_hidden_while_the_flag_is_off(leasing, monkeypatch):
    _lease()
    monkeypatch.delenv("ARTEMIS_DEVICE_LEASES")
    body = await _hosts()
    assert all(d["lease"] is None for d in body["devices"])
