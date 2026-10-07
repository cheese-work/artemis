"""Loopback ADB listeners and generation-scoped reconnect grace (B2)."""

import asyncio
from collections.abc import Callable
import logging
import time

from artemis.config.host_agent import host_agent_enabled
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.adb_gateway import Gateway
from artemis.runtime.host_endpoints import host_endpoints
from artemis.runtime.host_mux import Multiplexer
from artemis.runtime.host_protocol import CONTRACT

logger = logging.getLogger(__name__)


class HostTunnel:
    def __init__(self, host_id, generation, ws, shared, *, port=0):
        self.host_id = host_id
        self.generation = generation
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.shared = shared
        self.port = port
        self.mux = Multiplexer(generation)
        self.send_lock = asyncio.Lock()
        self.clients: dict[asyncio.StreamWriter, Gateway] = {}
        self.client_streams = {}
        self.tasks: set[asyncio.Task] = set()
        self.listener: asyncio.Server | None = None
        self.sender: asyncio.Task | None = None

    async def start(self) -> AdbEndpoint:
        self.listener = await asyncio.start_server(self.client, "127.0.0.1", self.port)
        self.sender = asyncio.create_task(self.send_frames())
        return AdbEndpoint.create(
            "127.0.0.1",
            self.listener.sockets[0].getsockname()[1],
            host_id=self.host_id,
            generation=self.generation,
        )

    async def send_json(self, message) -> None:
        async with self.send_lock:
            await asyncio.wait_for(self.ws.send_json(message), CONTRACT.dead_seconds)

    async def send_frames(self) -> None:
        try:
            while True:
                frame = await self.mux.next_frame()
                async with self.send_lock:
                    await asyncio.wait_for(
                        self.ws.send_bytes(frame.encode()), CONTRACT.dead_seconds
                    )
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            self.mux.close()

    async def client(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        gateway = Gateway(self.shared)
        self.clients[writer] = gateway
        stream = None
        try:
            stream = self.mux.open()
            self.client_streams[writer] = stream
            logger.info("event=stream_open host_id=%s stream_id=%d", self.host_id, stream.stream_id)
            await gateway.relay(reader, writer, stream)
        except (ConnectionError, OSError, ValueError):
            logger.debug("Host ADB stream closed", exc_info=True)
        finally:
            if stream is not None:
                if not self.mux.closed:
                    stream.close()
                logger.info(
                    "event=stream_close host_id=%s stream_id=%d bytes=%d ms=%d",
                    self.host_id,
                    stream.stream_id,
                    stream.bytes_sent + stream.bytes_received,
                    int((time.monotonic() - stream.started_at) * 1000),
                )
            self.clients.pop(writer, None)
            self.client_streams.pop(writer, None)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.tasks.discard(task)

    def unshare(self) -> None:
        shared = self.shared()
        for writer, gateway in list(self.clients.items()):
            if gateway.serial is not None and gateway.serial not in shared:
                self.client_streams[writer].close()
                writer.close()

    async def close(self) -> None:
        if self.loop is not asyncio.get_running_loop() and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
            await asyncio.wrap_future(future)
            return
        await self._close()

    async def _close(self) -> None:
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
        self.mux.close()
        tasks = list(self.tasks)
        if self.sender is not None:
            tasks.append(self.sender)
        for task in tasks:
            task.cancel()
        for writer in list(self.clients):
            writer.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def _set_status(host_id, status, reason):
    from apps.admin_console.services.host_registry import host_registry
    from apps.admin_console.services.host_admission import HostState, host_admission

    if status == "online" and host_admission.state_of(host_id) == HostState.OFFLINE:
        host_admission.heartbeat(host_id, HostState.ACTIVE)
    elif status != "online":
        host_admission.disconnect(host_id)

    if status == "offline":
        with host_registry._db() as connection:
            connection.execute(
                "UPDATE hosts SET status='offline', reason=?, since=? "
                "WHERE id=? AND status IN ('online','reconnecting') AND revoked_at IS NULL",
                (reason, host_registry.clock(), host_id),
            )
    else:
        host_registry._set_status(host_id, status, reason)


def _interrupt(session_id, reason="host_disconnected"):
    from apps.admin_console.services.task_queue_service import TaskQueueService

    TaskQueueService.interrupt_device_binding(session_id, reason)


def _note_loss(session_id):
    from apps.admin_console.services.task_queue_service import session_repo

    session_repo.lifecycle.note_loss(
        session_id, "host_disconnected", grace_seconds=CONTRACT.grace_seconds
    )


def _recover_loss(session_id):
    from apps.admin_console.services.task_queue_service import TaskQueueService, session_repo

    item = TaskQueueService._queue_item_for(session_id)
    if item.get("device_binding"):
        tunnel = host_tunnels.tunnels.get(item.get("host_id"))
        if tunnel is None or item.get("device_serial") not in tunnel.shared():
            _interrupt(session_id, "device_offline")
            return
    session_repo.lifecycle.recover_loss(session_id)


def _active_runs(host_id):
    try:
        from admin_console.core.state import state
    except ImportError:
        from apps.admin_console.core.state import state

    return {
        str(run.get("session_id") or run_id)
        for run_id, run in state.active_runs.items()
        if run.get("host_id") == host_id or run.get("adb_endpoint", {}).get("host_id") == host_id
    }


class HostTunnels:
    def __init__(
        self,
        *,
        endpoints=host_endpoints,
        clock: Callable[[], float] = time.monotonic,
        interrupt=_interrupt,
        set_status=_set_status,
        note_loss=_note_loss,
        recover_loss=_recover_loss,
    ):
        self.endpoints = endpoints
        self.clock = clock
        self.interrupt = interrupt
        self.set_status = set_status
        self.note_loss = note_loss
        self.recover_loss = recover_loss
        self.tunnels: dict[str, HostTunnel] = {}
        self.runs: dict[str, set[str]] = {}
        self.losses: dict[str, tuple[int, float, str]] = {}
        self.timers: dict[str, asyncio.Task] = {}
        self.generations: dict[str, int] = {}
        self.resolved: dict[str, asyncio.Event] = {}
        self.ports: dict[str, int] = {}

    def bind_run(self, host_id: str, session_id: str) -> None:
        self.runs.setdefault(host_id, set()).add(session_id)

    def release_run(self, host_id: str, session_id: str) -> None:
        self.runs.get(host_id, set()).discard(session_id)

    def reconnecting(self, host_id: str) -> float | None:
        loss = self.losses.get(host_id)
        return loss[1] if loss else None

    async def wait_for_run(self, session_id: str) -> None:
        for host_id, sessions in list(self.runs.items()):
            if session_id in sessions and host_id in self.losses:
                await self.resolved[host_id].wait()

    async def attach(self, host_id, generation, ws, shared) -> HostTunnel:
        if not host_agent_enabled():
            raise ValueError("Host agent is disabled")
        if generation <= self.generations.get(host_id, 0):
            raise ValueError("Stale connection generation")
        self.expire()
        self.generations[host_id] = generation
        previous = self.tunnels.pop(host_id, None)
        if previous is not None:
            self.endpoints.unregister(host_id, previous.generation)
            await previous.close()
        port = self.ports.get(host_id, 0) if self.runs.get(host_id) else 0
        tunnel = HostTunnel(host_id, generation, ws, shared, port=port)
        try:
            try:
                endpoint = await tunnel.start()
            except OSError:
                if not port:
                    raise
                await tunnel.close()
                if self.generations.get(host_id) != generation:
                    raise ValueError("Superseded connection generation")
                tunnel = HostTunnel(host_id, generation, ws, shared)
                endpoint = await tunnel.start()
            if self.generations.get(host_id) != generation:
                raise ValueError("Superseded connection generation")
        except (OSError, ValueError, asyncio.CancelledError):
            await tunnel.close()
            raise
        self.tunnels[host_id] = tunnel
        self.ports[host_id] = endpoint.port
        self.endpoints.register(endpoint)
        for session_id in self.runs.get(host_id, set()):
            self.recover_loss(session_id)
        self.losses.pop(host_id, None)
        self.resolved.setdefault(host_id, asyncio.Event()).set()
        timer = self.timers.pop(host_id, None)
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        self.set_status(host_id, "online", None)
        return tunnel

    async def disconnect(self, host_id, generation, reason):
        tunnel = self.tunnels.get(host_id)
        if tunnel is None or tunnel.generation != generation:
            return
        self.tunnels.pop(host_id)
        self.endpoints.unregister(host_id, generation)
        self.runs.setdefault(host_id, set()).update(_active_runs(host_id))
        if not self.runs[host_id] or reason in {"auth_expired", "revoked", "server_restarted"}:
            interrupt_reason = (
                "server_restarted" if reason == "server_restarted" else "auth_expired"
            )
            for session_id in self.runs.pop(host_id, set()):
                self.interrupt(session_id, interrupt_reason)
            self.set_status(host_id, "offline", reason)
            await tunnel.close()
            return
        self.losses[host_id] = (generation, self.clock() + CONTRACT.grace_seconds, reason)
        self.resolved.setdefault(host_id, asyncio.Event()).clear()
        for session_id in self.runs[host_id]:
            self.note_loss(session_id)
        self.set_status(host_id, "reconnecting", reason)
        logger.info(
            "event=host_lost host_id=%s grace_s=%d reason=%s",
            host_id,
            CONTRACT.grace_seconds,
            reason,
        )
        self.timers[host_id] = asyncio.create_task(self._wait_grace(host_id, generation))
        await tunnel.close()

    async def abort_host(self, host_id: str, reason: str) -> None:
        tunnel = self.tunnels.get(host_id)
        if tunnel is not None:
            await self.disconnect(host_id, tunnel.generation, reason)
        else:
            for session_id in self.runs.pop(host_id, set()):
                self.interrupt(session_id, reason)
        self.losses.pop(host_id, None)
        self.resolved.setdefault(host_id, asyncio.Event()).set()
        timer = self.timers.pop(host_id, None)
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)

    async def _wait_grace(self, host_id, generation):
        try:
            await asyncio.sleep(CONTRACT.grace_seconds)
            if self.losses.get(host_id, (None,))[0] == generation:
                self.expire()
        finally:
            if self.timers.get(host_id) is asyncio.current_task():
                self.timers.pop(host_id, None)

    def expire(self) -> None:
        for host_id, (_, deadline, reason) in list(self.losses.items()):
            if self.clock() >= deadline:
                self.losses.pop(host_id)
                for session_id in self.runs.pop(host_id, set()):
                    self.interrupt(session_id)
                self.resolved[host_id].set()
                self.set_status(host_id, "offline", reason)

    async def close(self) -> None:
        for host_id, tunnel in list(self.tunnels.items()):
            self.endpoints.unregister(host_id, tunnel.generation)
            await tunnel.close()
        self.tunnels.clear()
        for timer in self.timers.values():
            timer.cancel()
        await asyncio.gather(*self.timers.values(), return_exceptions=True)
        self.timers.clear()
        self.losses.clear()
        self.runs.clear()
        self.generations.clear()
        for event in self.resolved.values():
            event.set()
        self.resolved.clear()
        self.ports.clear()


host_tunnels = HostTunnels()
