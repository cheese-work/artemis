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

"""The live screen stream shows a caller only phones they may use (CHE-1242, BUG-001).

The stream follows the first phone the shared adb server lists. A private phone a
QA connected from their browser appears there too, so the stream must check who
owns the phone it is showing before it sends a single frame.
"""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from apps.admin_console.core.access_control import AccessConfig
from apps.admin_console.server import app
from apps.admin_console.services.bridge_session_service import BridgeSession, bridge_session_service
from apps.admin_console.services.device_stream_service import (
    DeviceStreamService,
    device_stream_service,
)

QA1 = "qa1@example.com"
QA2 = "qa2@example.com"
ADMIN = "admin@example.com"
QA1_PHONE = "127.0.0.1:41001"
SHARED = "emulator-5554"


@pytest.fixture
def cloudflare(monkeypatch):
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
    verifier.verify = AsyncMock(side_effect=lambda token, _config: {"email": token})
    monkeypatch.setattr(app.state, "access_verifier", verifier)
    session = BridgeSession(
        session_id="s41001", port=41001, created_at=0.0, expires_at=float("inf"), owner=QA1
    )
    monkeypatch.setattr(bridge_session_service, "_sessions", {"s41001": session})


async def _state(email: str | None, serial: str | None, monkeypatch) -> dict:
    monkeypatch.setattr(device_stream_service, "get_device_serial", AsyncMock(return_value=serial))
    headers = {"Cf-Access-Jwt-Assertion": email} if email else {}
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 51000)), base_url="http://localhost"
    ) as client:
        return (await client.get("/api/stream/device-state", headers=headers)).json()


@pytest.mark.asyncio
async def test_the_owner_and_an_admin_see_a_private_phone_in_the_stream_state(
    cloudflare, monkeypatch
):
    assert (await _state(QA1, QA1_PHONE, monkeypatch))["serial"] == QA1_PHONE
    assert (await _state(ADMIN, QA1_PHONE, monkeypatch))["serial"] == QA1_PHONE


@pytest.mark.asyncio
async def test_another_qa_is_told_nothing_about_a_private_phone(cloudflare, monkeypatch):
    state = await _state(QA2, QA1_PHONE, monkeypatch)

    assert state == {"connected": False, "serial": None, "live_stream_url": None}


@pytest.mark.asyncio
async def test_a_shared_phone_is_in_the_stream_state_for_everyone(cloudflare, monkeypatch):
    assert (await _state(QA2, SHARED, monkeypatch))["serial"] == SHARED


async def _frames(service: DeviceStreamService, may_use, serial, count_seconds=0.3) -> list[bytes]:
    """Frames one listener receives while the capture loop shows ``serial``."""
    service._latest_frame = b"frame-of-" + str(serial).encode()
    service._frame_serial = serial
    service._last_frame_time = 1.0
    generator = service.mjpeg_frame_generator(may_use)
    received: list[bytes] = []
    try:
        import asyncio

        received.append(await asyncio.wait_for(generator.__anext__(), count_seconds))
    except asyncio.TimeoutError:
        pass
    finally:
        await generator.aclose()
    return received


@pytest.mark.asyncio
async def test_a_frame_is_sent_only_when_the_listener_may_use_its_phone(monkeypatch):
    service = DeviceStreamService()
    monkeypatch.setattr(service, "_capture_loop", AsyncMock())

    allowed = await _frames(service, lambda serial: serial == QA1_PHONE, QA1_PHONE)
    refused = await _frames(service, lambda serial: serial == "someone-else", QA1_PHONE)
    unknown = await _frames(service, lambda serial: True, None)

    assert allowed and b"frame-of-" + QA1_PHONE.encode() in allowed[0]
    assert refused == []
    assert unknown == [], (
        "a frame that cannot be attributed to a phone is never sent to a scoped caller"
    )


@pytest.mark.asyncio
async def test_an_unscoped_listener_still_gets_every_frame(monkeypatch):
    service = DeviceStreamService()
    monkeypatch.setattr(service, "_capture_loop", AsyncMock())

    assert await _frames(service, None, None)
