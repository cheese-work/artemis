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

"""Device Pool Manager for discovering, tracking, and allocating mobile devices."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import subprocess
import threading
import time
from typing import Any

from artemis.runtime.adb_endpoint import AdbEndpoint, current_adb_endpoint
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DeviceStatus:
    """State and lock allocation metadata for a connected device."""

    serial: str
    state: str  # "device", "offline", "unauthorized", etc.
    model: str | None = None
    product: str | None = None
    is_emulator: bool = False
    is_busy: bool = False
    active_pid: int | None = None
    active_task_desc: str | None = None
    active_session_id: str | None = None
    acquired_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "is_emulator": self.is_emulator,
            "is_busy": self.is_busy,
            "active_pid": self.active_pid,
            "active_task_desc": self.active_task_desc,
            "active_session_id": self.active_session_id,
            "acquired_at": self.acquired_at,
        }


RawDevice = tuple[str, str, str | None, str | None]

#: ``(pool id, endpoint)`` an enumeration is pinned to. A task or coroutine started inside
#: the pin inherits it, so the query, the cache write, the stale fallback and the busy-state
#: scope of one listing all name the endpoint the listing began on.
_PINNED: ContextVar[tuple[int, AdbEndpoint] | None] = ContextVar("device_pool_pinned", default=None)


@dataclass
class _Snapshot:
    """Discovery state of one adb endpoint: cache, warm flag, in-flight query."""

    raw: list[RawDevice] | None = None
    at: float = 0.0
    warmed: bool = False
    inflight: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = field(default=None)


class DevicePool:
    """Manages discovery and assignment across the devices of one adb endpoint.

    A pool bound to an endpoint (:meth:`for_endpoint`) always talks to that adb server.
    An unbound pool, like the module-level :data:`device_pool`, follows the process's
    adb endpoint and keeps a separate snapshot per endpoint, so switching the adb
    server preference never answers with the previous server's devices. Every query
    names its server explicitly (``-H``/``-P``); busy state is matched against the
    endpoint's lock scope, so a serial in use on one server is not busy on another.
    """

    # A snapshot younger than this is returned without touching adb, so a burst
    # of concurrent enumerations (UI polling, readiness probes, submissions)
    # collapses into a single `adb devices` call.
    CACHE_TTL = 1.0
    # When enumeration fails outright (adb server restarting, subprocess error),
    # the last successful snapshot keeps answering for this long. Real
    # disconnects are not failures -- adb reports them as a successful listing
    # without the device -- so serving stale here only bridges server-level blips.
    STALE_ON_ERROR_TTL = 10.0
    # Until one enumeration has succeeded, `adb devices` may need to spawn the
    # adb server itself, which routinely exceeds the hot-path budget.
    HOT_QUERY_TIMEOUT = 2.0
    COLD_QUERY_TIMEOUT = 8.0

    _bound_pools: dict[AdbEndpoint, DevicePool] = {}
    _bound_pools_lock = threading.Lock()

    def __init__(self, adb_path: str | None = None, endpoint: AdbEndpoint | None = None):
        self._adb_path = adb_path
        self._bound_endpoint = endpoint
        self._cache_lock = threading.Lock()
        self._snapshots: dict[str, _Snapshot] = {}
        self._sync_query_gate = threading.Lock()

    @classmethod
    def for_endpoint(cls, endpoint: AdbEndpoint) -> DevicePool:
        """The pool of one adb endpoint (one instance per endpoint, shared)."""
        with cls._bound_pools_lock:
            pool = cls._bound_pools.get(endpoint)
            if pool is None:
                pool = cls._bound_pools[endpoint] = cls(endpoint=endpoint)
            return pool

    def pool_for(self, endpoint: AdbEndpoint) -> DevicePool:
        """This pool when it already serves ``endpoint``, else the shared pool bound to it."""
        if self._endpoint() == endpoint:
            return self
        return self.for_endpoint(endpoint)

    def _endpoint(self) -> AdbEndpoint:
        if self._bound_endpoint is not None:
            return self._bound_endpoint
        pinned = _PINNED.get()
        if pinned is not None and pinned[0] == id(self):
            return pinned[1]
        return current_adb_endpoint()

    @contextmanager
    def _pinned(self) -> Iterator[AdbEndpoint]:
        """Resolve the endpoint once for a whole listing (nested pins reuse the outer one)."""
        endpoint = self._endpoint()
        token = _PINNED.set((id(self), endpoint))
        try:
            yield endpoint
        finally:
            _PINNED.reset(token)

    def _transport(self) -> EndpointTransport:
        endpoint = self._endpoint()
        if self._adb_path:
            return EndpointTransport(endpoint, adb_path=self._adb_path)
        return EndpointTransport.shared(endpoint)

    def _snapshot(self) -> _Snapshot:
        """Snapshot of the current endpoint. Callers hold ``_cache_lock`` to mutate it."""
        identity = self._endpoint().identity
        snapshot = self._snapshots.get(identity)
        if snapshot is None:
            snapshot = self._snapshots[identity] = _Snapshot()
        return snapshot

    @property
    def _cached_raw(self) -> list[RawDevice] | None:
        return self._snapshot().raw

    @property
    def _warmed(self) -> bool:
        return self._snapshot().warmed

    @property
    def _async_inflight(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Task] | None:
        return self._snapshot().inflight

    @_async_inflight.setter
    def _async_inflight(self, value: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None) -> None:
        self._snapshot().inflight = value

    def _resolve_adb(self) -> str | None:
        return self._adb_path or EndpointTransport.adb_binary()

    def _current_query_timeout(self) -> float:
        return self.HOT_QUERY_TIMEOUT if self._warmed else self.COLD_QUERY_TIMEOUT

    def _query_adb_devices_sync(
        self, timeout: float | None = None
    ) -> list[tuple[str, str, str | None, str | None]] | None:
        """Run `adb devices -l` synchronously. Returns None when the query
        itself failed, as opposed to an empty list of attached devices."""
        if not self._resolve_adb():
            return None
        try:
            res = self._transport().run(
                ["devices", "-l"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self._current_query_timeout(),
                check=False,
            )
            if res.returncode != 0:
                return None
            return self._parse_device_lines(res.stdout.splitlines())
        except Exception as exc:
            logger.debug(f"Error querying adb devices: {exc}")
            return None

    async def _query_adb_devices_async(
        self, timeout: float | None = None
    ) -> list[tuple[str, str, str | None, str | None]] | None:
        """Run `adb devices -l` asynchronously. Returns None when the query
        itself failed, as opposed to an empty list of attached devices."""
        if not self._resolve_adb():
            return None
        proc = None
        try:
            proc = await self._transport().create_subprocess(
                ["devices", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout if timeout is not None else self._current_query_timeout(),
            )
            if proc.returncode != 0:
                return None
            return self._parse_device_lines(stdout.decode(errors="replace").splitlines())
        except Exception as exc:
            logger.debug(f"Error querying adb devices asynchronously: {exc}")
            if proc is not None:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    # Process already exited; nothing left to clean up.
                    pass
            return None

    @staticmethod
    def _parse_device_lines(lines: list[str]) -> list[tuple[str, str, str | None, str | None]]:
        results: list[tuple[str, str, str | None, str | None]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            model = None
            product = None
            for token in parts[2:]:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1].replace("_", " ")
                elif token.startswith("product:"):
                    product = token.split(":", 1)[1]
            results.append((serial, state, model, product))
        return results

    def _cached_snapshot(
        self, *, allow_stale: bool
    ) -> list[tuple[str, str, str | None, str | None]] | None:
        with self._cache_lock:
            snapshot = self._snapshot()
            if snapshot.raw is None:
                return None
            age = time.monotonic() - snapshot.at
            if age <= self.CACHE_TTL or (allow_stale and age <= self.STALE_ON_ERROR_TTL):
                return list(snapshot.raw)
        return None

    def _store_snapshot(self, raw: list[tuple[str, str, str | None, str | None]]) -> None:
        with self._cache_lock:
            snapshot = self._snapshot()
            snapshot.raw = list(raw)
            snapshot.at = time.monotonic()
            snapshot.warmed = True

    def _enumerate_sync(self) -> list[tuple[str, str, str | None, str | None]] | None:
        with self._pinned():
            return self._enumerate_sync_pinned()

    def _enumerate_sync_pinned(self) -> list[tuple[str, str, str | None, str | None]] | None:
        """Return the raw enumeration, or None when the query failed and no
        usable snapshot exists -- never an ambiguous empty list on failure."""
        cached = self._cached_snapshot(allow_stale=False)
        if cached is not None:
            return cached
        # The gate serializes concurrent enumerations: followers block until the
        # leader finishes, then hit the snapshot it just stored.
        with self._sync_query_gate:
            cached = self._cached_snapshot(allow_stale=False)
            if cached is not None:
                return cached
            raw = self._query_adb_devices_sync()
            if raw is None:
                return self._cached_snapshot(allow_stale=True)
            self._store_snapshot(raw)
            return raw

    async def _enumerate_async(self) -> list[tuple[str, str, str | None, str | None]] | None:
        with self._pinned():
            return await self._enumerate_async_pinned()

    async def _enumerate_async_pinned(
        self,
    ) -> list[tuple[str, str, str | None, str | None]] | None:
        cached = self._cached_snapshot(allow_stale=False)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        inflight = self._async_inflight
        if inflight is not None and inflight[0] is loop and not inflight[1].done():
            # Shield followers so one cancelled waiter cannot abort the shared query.
            return await asyncio.shield(inflight[1])
        task = loop.create_task(self._enumerate_async_uncached())
        self._async_inflight = (loop, task)
        try:
            return await task
        finally:
            if self._async_inflight is not None and self._async_inflight[1] is task:
                self._async_inflight = None

    async def _enumerate_async_uncached(
        self,
    ) -> list[tuple[str, str, str | None, str | None]] | None:
        raw = await self._query_adb_devices_async()
        if raw is None:
            return self._cached_snapshot(allow_stale=True)
        self._store_snapshot(raw)
        return raw

    async def _start_adb_server(self, timeout: float) -> None:
        """Best-effort bounded `adb start-server` so later queries hit a warm daemon.

        Local-only: it starts *this* machine's server, so it never runs for a remote
        or host endpoint (whose server is not ours to start).
        """
        if not self._resolve_adb():
            return
        transport = self._transport()
        if not transport.is_local:
            logger.debug(f"Not starting an adb server for {transport.endpoint.identity}")
            return
        proc = None
        try:
            proc = await transport.create_subprocess(
                ["start-server"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except Exception as exc:
            logger.debug(f"adb start-server warm-up did not complete: {exc}")
            if proc is not None:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    # Process already exited; nothing left to clean up.
                    pass

    async def warm_up_async(
        self,
        *,
        server_timeout: float = 10.0,
        settle_timeout: float = 3.0,
        poll_interval: float = 0.5,
    ) -> bool:
        """Start the adb server and complete one successful enumeration.

        Meant to run once before an entrypoint starts accepting task
        submissions, so the first requests never race an adb cold start.
        Devices reconnect asynchronously after the server comes up, so the
        settle window keeps polling until at least one device reaches the
        ready state (or the window closes). Returns True once any enumeration
        succeeded -- zero attached devices is still a warm pool.
        """
        with self._pinned():
            return await self._warm_up_pinned(server_timeout, settle_timeout, poll_interval)

    async def _warm_up_pinned(
        self, server_timeout: float, settle_timeout: float, poll_interval: float
    ) -> bool:
        if self._resolve_adb() is None:
            return False
        await self._start_adb_server(timeout=server_timeout)
        deadline = time.monotonic() + max(settle_timeout, 0.0)
        succeeded = False
        while True:
            raw = await self._query_adb_devices_async()
            if raw is not None:
                succeeded = True
                self._store_snapshot(raw)
                if any(state == "device" for _, state, _, _ in raw):
                    return True
            if time.monotonic() >= deadline:
                return succeeded
            await asyncio.sleep(poll_interval)

    def _build_statuses(
        self, raw_devices: list[tuple[str, str, str | None, str | None]]
    ) -> list[DeviceStatus]:
        active_owners = DeviceExecutionLock.get_active_owners()
        lock_scope = self._endpoint().lock_scope

        devices: list[DeviceStatus] = []
        for serial, state, model, product in raw_devices:
            is_emu = (
                serial.startswith("emulator-") or "127.0.0.1" in serial or "localhost" in serial
            )
            owner = self._owner_on_endpoint(active_owners, serial, lock_scope)

            status = DeviceStatus(
                serial=serial,
                state=state,
                model=model,
                product=product,
                is_emulator=is_emu,
                is_busy=owner is not None,
                active_pid=owner.pid if owner else None,
                active_task_desc=owner.description if owner else None,
                active_session_id=owner.session_id if owner else None,
                acquired_at=owner.acquired_at if owner else None,
            )
            devices.append(status)
        return devices

    @staticmethod
    def _owner_on_endpoint(active_owners: dict, serial: str, lock_scope: str):
        """The lock owner of ``serial`` on the endpoint scoped by ``lock_scope``.

        A device is a (server, serial) pair, so an owner that holds the same serial on a
        different adb server does not make this one busy. Owners without a recorded
        scope (written before scoping existed) match any endpoint.
        """
        normalize = DeviceExecutionLock._normalize_device_id
        owner = active_owners.get(DeviceExecutionLock._normalize_lock_id(serial, lock_scope))
        if owner is None:
            owner = active_owners.get(normalize(serial))
            if owner is not None and owner.lock_scope:
                if normalize(owner.lock_scope) != normalize(lock_scope):
                    owner = None
        return owner

    def list_devices(self) -> list[DeviceStatus]:
        """Synchronously list all connected devices along with their active lock state.

        The raw adb enumeration may be served from a short-lived snapshot;
        lock ownership is always computed fresh. A failed enumeration collapses
        to an empty list -- use try_list_devices when the caller must tell the
        two apart.
        """
        with self._pinned():
            return self._build_statuses(self._enumerate_sync() or [])

    async def list_devices_async(self) -> list[DeviceStatus]:
        """Asynchronously list all connected devices along with their active lock state.

        The raw adb enumeration may be served from a short-lived snapshot;
        lock ownership is always computed fresh. A failed enumeration collapses
        to an empty list -- use try_list_devices_async when the caller must tell
        the two apart.
        """
        with self._pinned():
            return self._build_statuses(await self._enumerate_async() or [])

    def try_list_devices(self) -> list[DeviceStatus] | None:
        """Like list_devices, but returns None when enumeration failed and no
        usable snapshot exists, so callers can distinguish "could not ask adb"
        from "adb answered: no devices attached"."""
        with self._pinned():
            raw = self._enumerate_sync()
            return None if raw is None else self._build_statuses(raw)

    async def try_list_devices_async(self) -> list[DeviceStatus] | None:
        """Async variant of try_list_devices."""
        with self._pinned():
            raw = await self._enumerate_async()
            return None if raw is None else self._build_statuses(raw)

    @staticmethod
    def _explicit_serial_error(
        requested_serial: str, devices: list[DeviceStatus] | None
    ) -> str | None:
        # Fail open on an indeterminate or empty enumeration (adb missing, server
        # blip, devices still handshaking): the submission proceeds and fails
        # downstream with a clear no-device error instead of a false hard reject.
        if not devices:
            return None
        norm = DeviceExecutionLock._normalize_device_id
        state_by_serial = {norm(d.serial): d.state for d in devices}
        dev_state = state_by_serial.get(norm(requested_serial))
        if dev_state == "device":
            return None
        if dev_state is None:
            return (
                f"Device '{requested_serial}' is not connected. "
                f"Attached devices: {sorted(state_by_serial)}."
            )
        return f"Device '{requested_serial}' is attached but not ready (state: '{dev_state}')."

    def validate_explicit_serial(self, requested_serial: str) -> str | None:
        """Return a rejection message for an explicitly requested serial, or None.

        Only a successful, non-empty enumeration may reject: it is the single
        authority for the strict device-binding check shared by the admission
        paths (admin console router/queue and the MCP task runner).
        """
        return self._explicit_serial_error(requested_serial, self.try_list_devices())

    async def validate_explicit_serial_async(self, requested_serial: str) -> str | None:
        """Async variant of validate_explicit_serial."""
        return self._explicit_serial_error(requested_serial, await self.try_list_devices_async())

    def get_ready_devices(self) -> list[DeviceStatus]:
        """Return devices in authorized 'device' state."""
        return [d for d in self.list_devices() if d.state == "device"]

    def get_idle_devices(self) -> list[DeviceStatus]:
        """Return ready devices that are not currently holding an execution lock."""
        return [d for d in self.list_devices() if d.state == "device" and not d.is_busy]

    def get_claimed_serials(self) -> set[str]:
        """Return device serials Artemis currently claims on the ADB server.

        A device is claimed while any Artemis process holds its execution lock
        or has a live pending queue reservation targeting it. Placeholder
        reservations not yet bound to a concrete device ("pending"/"any")
        claim nothing. Read-only: computed purely from cross-process lock and
        queue metadata, without any adb traffic.
        """
        claimed: set[str] = set()
        scope = DeviceExecutionLock._normalize_device_id(self._endpoint().lock_scope)

        def on_endpoint(recorded_scope: object) -> bool:
            # Unscoped records predate scoping and count for every endpoint.
            if not recorded_scope:
                return True
            return DeviceExecutionLock._normalize_device_id(str(recorded_scope)) == scope

        try:
            for owner in DeviceExecutionLock.get_active_owners().values():
                if owner.device_id and on_endpoint(owner.lock_scope):
                    claimed.add(str(owner.device_id))
            for item in DeviceExecutionLock.get_queued_tasks():
                serial = item.get("device_serial")
                if serial and on_endpoint(item.get("lock_scope")):
                    claimed.add(str(serial))
        except Exception as exc:
            logger.debug(f"Could not compute claimed device serials: {exc}")
        claimed.discard("pending")
        claimed.discard("any")
        return claimed

    def select_device(self, preferred_serial: str | None = None) -> str | None:
        """Select a target device serial for task execution.

        If `preferred_serial` is provided:
            Returns preferred_serial if it is present or online.
        Otherwise:
            Selects the first idle/unlocked ready device. If all are busy,
            selects the first ready device so the task will queue on it.
            Returns None if no devices are attached.
        """
        return self._pick_device(self.list_devices(), preferred_serial)

    async def select_device_async(self, preferred_serial: str | None = None) -> str | None:
        """Select a device without blocking the caller on ADB enumeration."""
        return self._pick_device(await self.list_devices_async(), preferred_serial)

    def _pick_device(
        self, all_devs: list[DeviceStatus], preferred_serial: str | None
    ) -> str | None:
        ready_devs = [d for d in all_devs if d.state == "device"]

        if preferred_serial:
            # Check if preferred is available or exists
            for dev in all_devs:
                if dev.serial == preferred_serial:
                    return dev.serial
            # If not detected by ADB, still return it so ADB can attempt connection
            return preferred_serial

        # Pick first idle device
        for dev in ready_devs:
            if not dev.is_busy:
                return dev.serial

        # If all busy, return the first ready device (it will queue)
        if ready_devs:
            return ready_devs[0].serial

        # Fallback to any attached device if none ready
        if all_devs:
            return all_devs[0].serial

        return None


# Global device pool instance
device_pool = DevicePool()
