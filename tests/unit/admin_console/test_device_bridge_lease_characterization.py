"""Characterization tests for the device-bridge lease (CHE-1156, audit of CHE-1088 / #51).

Pins two behaviours the original PR shipped without a test written first:
browser-to-ADB traffic alone renews the idle lease, and the lifetime-cap
environment setting reaches sessions the service creates. Negative controls
are recorded in the PR description.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from apps.admin_console.routers import device_bridge
import apps.admin_console.services.bridge_session_service as bridge_session_service_module
from apps.admin_console.services.bridge_session_service import (
    DEFAULT_SESSION_MAX_LIFETIME_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS,
    BridgeSession,
    BridgeSessionService,
)

CAP_ENV = "ARTEMIS_BRIDGE_SESSION_MAX_LIFETIME_SECONDS"
TTL_ENV = "ARTEMIS_BRIDGE_SESSION_TTL_SECONDS"
CLOSE_SESSION_EXPIRED = 4008


def _adb_packet(command: bytes = b"CNXN") -> bytes:
    word = int.from_bytes(command, "little")
    return struct.pack("<6I", word, 0, 0, 0, 0, word ^ 0xFFFFFFFF)


class FakeClock:
    """Monotonic clock plus a sleep that only returns when the test advances time."""

    def __init__(self) -> None:
        self.current = 1000.0
        self.changed = asyncio.Event()
        self.sleep_started = asyncio.Event()

    def monotonic(self) -> float:
        return self.current

    async def advance(self, seconds: float) -> None:
        self.current += seconds
        self.changed.set()
        self.changed = asyncio.Event()
        for _ in range(10):  # let woken tasks run
            await asyncio.sleep(0)

    async def sleep(self, seconds: float) -> None:
        deadline = self.current + seconds
        self.sleep_started.set()
        while self.current < deadline:
            await self.changed.wait()


class FakeWebSocket:
    def __init__(self) -> None:
        self.scope = {"client": ("127.0.0.1", 50000)}
        self.client = ("127.0.0.1", 50000)
        self.messages: asyncio.Queue = asyncio.Queue()
        self.sent_json: asyncio.Queue = asyncio.Queue()
        self.closed_code: int | None = None
        self.closed = asyncio.Event()

    async def accept(self):
        pass

    async def send_json(self, message):
        await self.sent_json.put(message)

    async def send_bytes(self, message):
        pass

    async def receive(self):
        return await self.messages.get()

    async def close(self, code):
        self.closed_code = code
        self.closed.set()

    async def browser_sends(self, packet: bytes) -> None:
        await self.messages.put({"type": "websocket.receive", "bytes": packet})


class SinkWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        pass


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(
        bridge_session_service_module,
        "time",
        type("FakeTime", (), {"monotonic": staticmethod(fake.monotonic)}),
    )
    monkeypatch.setattr(device_bridge, "_sleep", fake.sleep)
    return fake


@pytest.mark.asyncio
async def test_browser_to_tcp_only_traffic_renews_idle_lease(clock, monkeypatch):
    """Only the browser talks (nothing comes back from ADB) and the original 300 s deadline passes."""

    class Service:
        def __init__(self, session):
            self.session = session

        async def create_session(self):
            return self.session

        async def connect(self, session):
            return session.serial

        async def revoke(self, _session_id):
            pass

    session = BridgeSession(
        session_id="browser-only",
        port=43210,
        reader=asyncio.StreamReader(),  # never fed: no TCP-to-browser traffic
        writer=SinkWriter(),
        created_at=clock.monotonic(),
        expires_at=clock.monotonic() + 300,
        idle_timeout_seconds=300,
    )
    session.connected.set()
    websocket = FakeWebSocket()
    monkeypatch.setattr(device_bridge, "bridge_session_service", Service(session))
    original_deadline = session.expires_at

    route = asyncio.create_task(device_bridge.open_bridge_session(websocket))
    try:
        await asyncio.wait_for(websocket.sent_json.get(), 1)  # session_leased
        await asyncio.wait_for(websocket.sent_json.get(), 1)  # device_attached
        await asyncio.wait_for(clock.sleep_started.wait(), 1)

        await clock.advance(250)
        await websocket.browser_sends(_adb_packet())
        await clock.advance(0)  # let the relay handle it before time moves on
        await clock.advance(250)  # t=500: past the original 300 s deadline
        assert not websocket.closed.is_set()
        assert len(session.writer.frames) == 1
        assert session.expires_at == original_deadline - 300 + 250 + 300  # renewed at t=250

        await websocket.browser_sends(_adb_packet())
        await clock.advance(0)  # let the relay handle it before time moves on
        await clock.advance(250)  # t=750: past the first renewal too
        assert not websocket.closed.is_set()
        assert session.expires_at > clock.monotonic()

        await clock.advance(301)  # silent for longer than the idle timeout
        await asyncio.wait_for(websocket.closed.wait(), 1)
        assert websocket.closed_code == CLOSE_SESSION_EXPIRED
        assert session.close_reason == "idle"
    finally:
        if not route.done():
            await websocket.messages.put({"type": "websocket.receive", "text": "close"})
        await asyncio.gather(route, return_exceptions=True)


@pytest.mark.asyncio
async def test_lifetime_cap_environment_reaches_service_created_sessions(clock, monkeypatch):
    """A short cap from the environment closes a busy session although its idle lease keeps renewing."""
    monkeypatch.setenv(CAP_ENV, "600")
    monkeypatch.delenv(TTL_ENV, raising=False)

    async def fake_adb(*arguments):
        return f"connected to {arguments[1]}" if arguments[0] == "connect" else "disconnected"

    monkeypatch.setattr(bridge_session_service_module, "_run_adb_command", fake_adb)
    service = BridgeSessionService()
    monkeypatch.setattr(device_bridge, "bridge_session_service", service)
    websocket = FakeWebSocket()
    started = clock.monotonic()

    route = asyncio.create_task(device_bridge.open_bridge_session(websocket))
    adb_server_writer = None
    try:
        leased = await asyncio.wait_for(websocket.sent_json.get(), 1)
        session = service.live_sessions()[0]
        assert session.max_expires_at == started + 600
        assert session.idle_timeout_seconds == DEFAULT_SESSION_TTL_SECONDS
        # Act as the ADB server dialling the leased loopback listener.
        _, adb_server_writer = await asyncio.open_connection(
            "127.0.0.1", leased["listener"]["port"]
        )
        await asyncio.wait_for(websocket.sent_json.get(), 1)  # device_attached
        await asyncio.wait_for(clock.sleep_started.wait(), 1)

        for _ in range(2):  # traffic every 250 s keeps the idle lease alive
            await clock.advance(250)
            await websocket.browser_sends(_adb_packet())
            await clock.advance(0)
            await asyncio.sleep(0.05)  # real I/O through the loopback socket
            assert not websocket.closed.is_set()

        await clock.advance(101)  # t=601: past the cap, inside the renewed idle lease
        await asyncio.wait_for(websocket.closed.wait(), 1)
        assert websocket.closed_code == CLOSE_SESSION_EXPIRED
        assert session.close_reason == "cap"
    finally:
        if not route.done():
            await websocket.messages.put({"type": "websocket.receive", "text": "close"})
        await asyncio.gather(route, return_exceptions=True)
        if adb_server_writer is not None:
            adb_server_writer.close()


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "nan-ish", ""])
@pytest.mark.asyncio
async def test_invalid_or_non_positive_cap_falls_back_to_default(clock, monkeypatch, raw):
    monkeypatch.setenv(CAP_ENV, raw)
    service = BridgeSessionService()

    session = await service.create_session()
    try:
        assert session.max_expires_at == clock.monotonic() + DEFAULT_SESSION_MAX_LIFETIME_SECONDS
    finally:
        await service.revoke(session.session_id)
