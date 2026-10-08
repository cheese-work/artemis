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

"""Session and ADB transport lifecycle for the browser device bridge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
import time
import uuid

from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.endpoint_transport import EndpointTransport

DEFAULT_SESSION_TTL_SECONDS = 300
DEFAULT_SESSION_MAX_LIFETIME_SECONDS = 4 * 60 * 60
ADB_COMMAND_TIMEOUT_SECONDS = 15
STREAM_CLOSE_TIMEOUT_SECONDS = 1
MAX_ADB_PACKET_BYTES = 1024 * 1024 + 24

logger = logging.getLogger(__name__)


def _session_ttl_seconds() -> float:
    raw = os.environ.get("ARTEMIS_BRIDGE_SESSION_TTL_SECONDS")
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_SESSION_TTL_SECONDS


def _session_max_lifetime_seconds() -> float:
    raw = os.environ.get("ARTEMIS_BRIDGE_SESSION_MAX_LIFETIME_SECONDS")
    if not raw:
        return DEFAULT_SESSION_MAX_LIFETIME_SECONDS
    try:
        lifetime = float(raw)
    except ValueError:
        return DEFAULT_SESSION_MAX_LIFETIME_SECONDS
    return lifetime if lifetime > 0 else DEFAULT_SESSION_MAX_LIFETIME_SECONDS


async def _run_adb_command(*arguments: str) -> str:
    """Run ``adb <arguments>`` against this computer's own adb server, whatever the preference.

    The bridge makes the *local* adb server dial the loopback listener (``adb connect
    127.0.0.1:<port>``); a run's endpoint, a host agent's tunnel included, is never the
    right server for that.
    """
    transport = EndpointTransport(AdbEndpoint.local())
    process = await transport.create_subprocess(
        arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=ADB_COMMAND_TIMEOUT_SECONDS
        )
    except TimeoutError as error:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise RuntimeError("adb command timed out") from error
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise

    output = (stdout + stderr).decode("utf-8", errors="replace").strip()
    if process.returncode:
        raise RuntimeError(f"adb {arguments[0]} failed: {output}")
    return output


@dataclass
class BridgeSession:
    """One loopback listener and ADB serial leased to a WebSocket."""

    session_id: str
    port: int = 0
    listener: asyncio.AbstractServer | None = None
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = 0.0
    idle_timeout_seconds: float = DEFAULT_SESSION_TTL_SECONDS
    max_expires_at: float = float("inf")
    adb_connect_attempted: bool = False
    revoked: bool = False
    close_reason: str | None = None
    close_code: int | None = None
    bytes_browser_to_device: int = 0
    bytes_device_to_browser: int = 0
    # Verified email of the person who connected the phone; None when no one was signed in.
    owner: str | None = None

    @property
    def serial(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= min(self.expires_at, self.max_expires_at)

    def remaining_seconds(self) -> float:
        return max(0.0, min(self.expires_at, self.max_expires_at) - time.monotonic())

    def renew(self) -> bool:
        now = time.monotonic()
        if now >= min(self.expires_at, self.max_expires_at):
            return False
        self.expires_at = min(now + self.idle_timeout_seconds, self.max_expires_at)
        return True

    def expiration_reason(self) -> str:
        return "cap" if time.monotonic() >= self.max_expires_at else "idle"


class BridgeSessionService:
    """Create, track, connect, and tear down device-bridge sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()

    def live_sessions(self) -> list[BridgeSession]:
        """Browser-attached phones, for the computer registry's device list."""
        return [s for s in self._sessions.values() if not s.is_expired]

    def owner_of(self, serial: str) -> str | None:
        """Who connected the browser phone at ``serial``; None when unowned or unknown."""
        # Admission and device locks match serials in this normalized form, so ownership must too.
        key = DeviceExecutionLock._normalize_device_id(serial)
        session = next(
            (
                s
                for s in self._sessions.values()
                if DeviceExecutionLock._normalize_device_id(s.serial) == key
            ),
            None,
        )
        return session.owner if session else None

    def newest_serial_of(self, owner: str | None) -> str | None:
        """The most recently connected live browser phone of ``owner``."""
        mine = [s for s in self.live_sessions() if owner is not None and s.owner == owner]
        return max(mine, key=lambda s: s.created_at).serial if mine else None

    async def create_session(self, owner: str | None = None) -> BridgeSession:
        created_at = time.monotonic()
        idle_timeout_seconds = _session_ttl_seconds()
        session = BridgeSession(
            session_id=uuid.uuid4().hex,
            owner=owner,
            created_at=created_at,
            idle_timeout_seconds=idle_timeout_seconds,
            max_expires_at=created_at + _session_max_lifetime_seconds(),
        )
        listener = await asyncio.start_server(
            lambda reader, writer: self._accept_connection(session, reader, writer),
            "127.0.0.1",
            0,
            limit=MAX_ADB_PACKET_BYTES,
        )
        registered = False
        try:
            sockets = listener.sockets or []
            if not sockets:
                raise RuntimeError("failed to bind device bridge listener")

            session.listener = listener
            session.port = int(sockets[0].getsockname()[1])
            session.expires_at = min(
                time.monotonic() + session.idle_timeout_seconds,
                session.max_expires_at,
            )
            async with self._lock:
                self._sessions[session.session_id] = session
            registered = True
            logger.info(
                "event=bridge_lease_created session_id=%s serial=%s",
                session.session_id,
                session.serial,
            )
            return session
        finally:
            if not registered:
                listener.close()
                await listener.wait_closed()

    async def connect(self, session: BridgeSession) -> str:
        session.adb_connect_attempted = True
        started = time.monotonic()
        try:
            output = await _run_adb_command("connect", session.serial)
            success = (f"connected to {session.serial}", f"already connected to {session.serial}")
            if not output.lower().startswith(tuple(message.lower() for message in success)):
                raise RuntimeError("adb connect returned an unsuccessful status")
        except Exception as error:
            logger.warning(
                "event=bridge_adb_connect session_id=%s result=failed serial=%s "
                "error_type=%s duration_ms=%d",
                session.session_id,
                session.serial,
                type(error).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise
        logger.info(
            "event=bridge_adb_connect session_id=%s result=connected duration_ms=%d serial=%s",
            session.session_id,
            int((time.monotonic() - started) * 1000),
            session.serial,
        )
        return session.serial

    async def revoke(self, session_id: str) -> None:
        """Disconnect ADB and release the listener and accepted stream."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return

        session.revoked = True
        session.close_reason = session.close_reason or "revoked"
        logger.info(
            "event=bridge_close session_id=%s serial=%s reason=%s close_code=%s "
            "bytes_browser_to_device=%d bytes_device_to_browser=%d duration_ms=%d",
            session.session_id,
            session.serial,
            session.close_reason,
            session.close_code,
            session.bytes_browser_to_device,
            session.bytes_device_to_browser,
            int((time.monotonic() - session.created_at) * 1000),
        )
        try:
            if session.adb_connect_attempted:
                started = time.monotonic()
                await _run_adb_command("disconnect", session.serial)
                logger.info(
                    "event=bridge_adb_disconnect session_id=%s result=disconnected "
                    "serial=%s duration_ms=%d",
                    session.session_id,
                    session.serial,
                    int((time.monotonic() - started) * 1000),
                )
        except Exception as error:
            logger.warning(
                "event=bridge_adb_disconnect session_id=%s result=failed serial=%s "
                "error_type=%s duration_ms=%d",
                session.session_id,
                session.serial,
                type(error).__name__,
                int((time.monotonic() - started) * 1000),
            )
        finally:
            if session.listener is not None:
                session.listener.close()
            if session.writer is not None:
                try:
                    session.writer.close()
                    await asyncio.wait_for(
                        session.writer.wait_closed(),
                        timeout=STREAM_CLOSE_TIMEOUT_SECONDS,
                    )
                except Exception as error:
                    logger.warning(
                        "Failed to close device bridge stream %s error_type=%s",
                        session.serial,
                        type(error).__name__,
                    )
                    session.writer.transport.abort()
            if session.listener is not None:
                try:
                    await asyncio.wait_for(
                        session.listener.wait_closed(),
                        timeout=STREAM_CLOSE_TIMEOUT_SECONDS,
                    )
                except Exception as error:
                    logger.warning(
                        "Failed to close device bridge listener %s error_type=%s",
                        session.serial,
                        type(error).__name__,
                    )

    async def get(self, session_id: str) -> BridgeSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def active_session_ids(self) -> set[str]:
        async with self._lock:
            return set(self._sessions.keys())

    @staticmethod
    async def _accept_connection(
        session: BridgeSession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if session.revoked or session.writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        session.reader = reader
        session.writer = writer
        session.connected.set()


bridge_session_service = BridgeSessionService()
