import asyncio
from dataclasses import dataclass

import pytest

from apps.admin_console.services.host_tunnel import HostTunnels
from artemis.runtime.host_protocol import CONTRACT, Frame, FrameKind
from artemis.runtime.host_endpoints import HostEndpointRegistry, HostOffline


@dataclass
class Clock:
    now: float = 100

    def __call__(self):
        return self.now


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()

    async def send_bytes(self, payload):
        await self.frames.put(Frame.decode(payload))


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    clock = Clock()
    outcomes, states = [], []
    service = HostTunnels(
        endpoints=HostEndpointRegistry(),
        clock=clock,
        interrupt=lambda session: outcomes.append(session),
        set_status=lambda host, status, reason: states.append((host, status, reason)),
        note_loss=lambda session: None,
        recover_loss=lambda session: None,
    )
    return service, clock, outcomes, states


@pytest.mark.asyncio
async def test_loopback_registered_and_listener_closed_on_loss(manager):
    service, _, _, states = manager
    socket = Socket()
    tunnel = await service.attach("lab", 1, socket, lambda: {"phone"})
    service.bind_run("lab", "run")
    endpoint = service.endpoints.resolve("lab")
    assert endpoint.host == "127.0.0.1"
    assert endpoint.generation == 1
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    writer.write(b"0009host:kill")
    await writer.drain()
    assert await reader.readexactly(4) == b"FAIL"
    await service.disconnect("lab", 1, "disconnected")
    assert tunnel.listener.is_serving() is False
    with pytest.raises(HostOffline):
        service.endpoints.resolve("lab")
    assert service.reconnecting("lab") == 130
    assert states[-1] == ("lab", "reconnecting", "disconnected")
    writer.close()
    await writer.wait_closed()
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disconnected", "timeout", "reset", "tcp_recovery"])
async def test_flap_harness_grace_and_exactly_one_interrupted(manager, reason):
    service, clock, outcomes, _ = manager
    await service.attach("lab", 1, Socket(), lambda: {"phone"})
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, reason)
    clock.now = 129.99
    service.expire()
    assert outcomes == []
    clock.now = 130
    service.expire()
    service.expire()
    await service.disconnect("lab", 1, reason)
    assert outcomes == ["run"]
    await service.close()


@pytest.mark.asyncio
async def test_reconnect_cancels_loss_and_stale_cleanup_cannot_close_new_listener(manager):
    service, clock, outcomes, _ = manager
    await service.attach("lab", 1, Socket(), lambda: {"phone"})
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, "disconnected")
    clock.now = 120
    newest = await service.attach("lab", 2, Socket(), lambda: {"phone"})
    await service.disconnect("lab", 1, "timeout")
    clock.now = 140
    service.expire()
    assert outcomes == []
    assert service.reconnecting("lab") is None
    assert newest.listener.is_serving()
    assert service.endpoints.resolve("lab").generation == 2
    await service.close()


@pytest.mark.asyncio
async def test_unshare_closes_selected_live_stream(manager):
    service, _, _, _ = manager
    shared = {"phone"}
    socket = Socket()
    tunnel = await service.attach("lab", 1, socket, lambda: shared)
    endpoint = service.endpoints.resolve("lab")
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    command = b"host:transport:phone"
    writer.write(f"{len(command):04x}".encode() + command)
    await writer.drain()
    opened = await socket.frames.get()
    assert opened.kind == FrameKind.OPEN
    import struct

    tunnel.mux.receive(
        Frame(FrameKind.ACK, 1, opened.stream_id, struct.pack("!I", CONTRACT.stream_credit))
    )
    await socket.frames.get()
    tunnel.mux.receive(Frame(FrameKind.DATA, 1, opened.stream_id, b"OKAY"))
    assert await reader.readexactly(4) == b"OKAY"
    shared.clear()
    tunnel.unshare()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    assert not tunnel.mux.streams
    writer.close()
    await writer.wait_closed()
    await service.close()


@pytest.mark.asyncio
async def test_flag_off_never_creates_listener(manager, monkeypatch):
    service, _, _, _ = manager
    monkeypatch.delenv("ARTEMIS_HOST_AGENT")
    with pytest.raises(ValueError, match="disabled"):
        await service.attach("lab", 1, Socket(), lambda: set())
    await service.close()


@pytest.mark.asyncio
async def test_grace_uses_a1_outbox_and_never_overrides_completion(manager, tmp_path, monkeypatch):
    import sqlite3
    import uuid
    from artemis.data_engine.storage import StorageManager
    from artemis.runtime.lifecycle import LifecycleAuthority
    from artemis.runtime import trace_store

    service, clock, _, _ = manager
    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    authority = LifecycleAuthority(database)
    running, completed = str(uuid.uuid4()), str(uuid.uuid4())
    with sqlite3.connect(database) as connection:
        for session in (running, completed):
            connection.execute(
                "INSERT INTO sessions (session_id, initial_goal, start_time, status) VALUES (?, 'fake', 1, 'running')",
                (session,),
            )
    authority.finish(completed, "completed")
    service.interrupt = lambda session: authority.interrupt(session, "host_disconnected")
    await service.attach("lab", 1, Socket(), lambda: set())
    for session in (running, completed):
        service.bind_run("lab", session)
    await service.disconnect("lab", 1, "reset")
    clock.now = 129
    service.expire()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (running,)
            ).fetchone()[0]
            == "running"
        )
    clock.now = 130
    service.expire()
    service.expire()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status, interrupt_reason FROM sessions WHERE session_id=?", (running,)
        ).fetchone() == ("interrupted", "host_disconnected")
        assert (
            connection.execute(
                "SELECT status FROM sessions WHERE session_id=?", (completed,)
            ).fetchone()[0]
            == "completed"
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE session_id=?", (running,)
            ).fetchone()[0]
            == 1
        )
    await service.close()


@pytest.mark.asyncio
async def test_concurrent_attach_cannot_publish_an_older_generation(manager, monkeypatch):
    from apps.admin_console.services.host_tunnel import HostTunnel

    service, _, _, _ = manager
    blocked, resume = asyncio.Event(), asyncio.Event()
    original = HostTunnel.start

    async def delayed_start(tunnel):
        if tunnel.generation == 1:
            blocked.set()
            await resume.wait()
        return await original(tunnel)

    monkeypatch.setattr(HostTunnel, "start", delayed_start)
    first = asyncio.create_task(service.attach("lab", 1, Socket(), lambda: set()))
    await blocked.wait()
    newest = await service.attach("lab", 2, Socket(), lambda: set())
    resume.set()
    with pytest.raises(ValueError, match="Superseded"):
        await first
    assert service.tunnels["lab"] is newest
    assert service.endpoints.resolve("lab").generation == 2
    await service.close()


@pytest.mark.asyncio
async def test_loss_defers_worker_failure_across_authorities_until_grace(tmp_path, monkeypatch):
    import sqlite3
    from artemis.data_engine.storage import StorageManager
    from artemis.runtime.lifecycle import LifecycleAuthority
    from artemis.runtime import trace_store

    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    clock = Clock()
    server, worker = (
        LifecycleAuthority(database, clock=clock),
        LifecycleAuthority(database, clock=clock),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status) VALUES ('run', 'fake', 1, 'running')"
        )
    assert server.note_loss("run", "host_disconnected", grace_seconds=CONTRACT.grace_seconds)
    outcome = worker.settle_worker_exit("run", 1, False)
    assert outcome.status == "running"
    assert not outcome.committed
    assert server.pending_events("run") == []
    clock.now = 130
    outcome = server.interrupt("run", "host_disconnected")
    assert outcome.status == "interrupted"
    assert len(server.pending_events("run")) == 1


@pytest.mark.asyncio
async def test_queue_wait_keeps_bound_run_until_loss_resolves(manager):
    service, clock, outcomes, _ = manager
    await service.attach("lab", 1, Socket(), lambda: set())
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, "reset")
    waiter = asyncio.create_task(service.wait_for_run("run"))
    await asyncio.sleep(0)
    assert not waiter.done()
    clock.now = 130
    service.expire()
    await asyncio.wait_for(waiter, 1)
    assert outcomes == ["run"]
    await service.close()


@pytest.mark.asyncio
async def test_auth_expiry_interrupts_without_grace(manager):
    service, _, _, _ = manager
    outcomes = []
    service.interrupt = lambda session, reason="host_disconnected": outcomes.append(
        (session, reason)
    )
    await service.attach("lab", 1, Socket(), lambda: set())
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, "auth_expired")
    assert service.reconnecting("lab") is None
    assert outcomes == [("run", "auth_expired")]
    await service.close()


@pytest.mark.asyncio
async def test_reconnect_preserves_loopback_address_for_existing_workers(manager):
    service, _, _, _ = manager
    await service.attach("lab", 1, Socket(), lambda: set())
    old = service.endpoints.resolve("lab")
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, "reset")
    await service.attach("lab", 2, Socket(), lambda: set())
    new = service.endpoints.resolve("lab")
    assert (new.host, new.port) == (old.host, old.port)
    assert new.generation == 2
    await service.close()


@pytest.mark.asyncio
async def test_revoke_during_grace_interrupts_immediately(manager):
    service, _, _, _ = manager
    outcomes = []
    service.interrupt = lambda session, reason="host_disconnected": outcomes.append(
        (session, reason)
    )
    await service.attach("lab", 1, Socket(), lambda: set())
    service.bind_run("lab", "run")
    await service.disconnect("lab", 1, "reset")
    await service.abort_host("lab", "auth_expired")
    assert outcomes == [("run", "auth_expired")]
    assert service.reconnecting("lab") is None
    await service.close()


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_loss_grace_does_not_defer_completion_or_cancel(tmp_path, monkeypatch, terminal):
    import sqlite3
    from artemis.data_engine.storage import StorageManager
    from artemis.runtime.lifecycle import LifecycleAuthority
    from artemis.runtime import trace_store

    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    authority = LifecycleAuthority(database, clock=Clock())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status) VALUES ('run', 'fake', 1, 'running')"
        )
    authority.note_loss("run", "host_disconnected", grace_seconds=30)
    assert authority.finish("run", terminal).status == terminal
    assert authority.interrupt("run", "host_disconnected").status == terminal
    assert len(authority.pending_events("run")) == 1


def test_recover_clears_pending_loss_before_worker_settlement(tmp_path, monkeypatch):
    import sqlite3
    from artemis.data_engine.storage import StorageManager
    from artemis.runtime.lifecycle import LifecycleAuthority
    from artemis.runtime import trace_store

    database = tmp_path / "sessions.db"
    StorageManager(database, tmp_path)
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path / "traces"))
    authority = LifecycleAuthority(database, clock=Clock())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status) VALUES ('run', 'fake', 1, 'running')"
        )
    authority.note_loss("run", "host_disconnected", grace_seconds=30)
    assert authority.recover_loss("run")
    assert authority.settle_worker_exit("run", 1, False).status == "failed"
