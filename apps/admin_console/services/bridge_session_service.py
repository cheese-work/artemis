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

from artemis.runtime.adb_endpoint import AdbEndpoint, AdbSession
from artemis.toolchain import find_adb

DEFAULT_SESSION_TTL_SECONDS = 300
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


async def _run_adb_command(*arguments: str) -> str:
    adb_session = AdbSession(AdbEndpoint.local(), adb_path=find_adb())
    process = await asyncio.create_subprocess_exec(
        *adb_session.command(arguments),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=adb_session.environment(),
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
    adb_connect_attempted: bool = False
    revoked: bool = False
    bytes_browser_to_device: int = 0
    bytes_device_to_browser: int = 0
    close_code: int | None = None
    close_reason: str = "session_cleanup"

    @property
    def serial(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())


class BridgeSessionService:
    """Create, track, connect, and tear down device-bridge sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self) -> BridgeSession:
        session = BridgeSession(session_id=uuid.uuid4().hex)
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
            session.expires_at = time.monotonic() + _session_ttl_seconds()
            async with self._lock:
                self._sessions[session.session_id] = session
            registered = True
            logger.info(
                "event=bridge_lease_created session_id=%s serial=%s ttl_seconds=%.3f",
                session.session_id,
                session.serial,
                session.remaining_seconds(),
            )
            return session
        finally:
            if not registered:
                listener.close()
                await listener.wait_closed()

    async def connect(self, session: BridgeSession) -> str:
        session.adb_connect_attempted = True
        started_at = time.monotonic()
        try:
            output = await _run_adb_command("connect", session.serial)
            success = (f"connected to {session.serial}", f"already connected to {session.serial}")
            if not output.lower().startswith(tuple(message.lower() for message in success)):
                raise RuntimeError("adb connect returned an unsuccessful status")
        except Exception as error:
            logger.warning(
                "event=bridge_adb_connect session_id=%s serial=%s result=failed "
                "error_type=%s duration_ms=%d",
                session.session_id,
                session.serial,
                type(error).__name__,
                round((time.monotonic() - started_at) * 1000),
            )
            raise
        logger.info(
            "event=bridge_adb_connect session_id=%s serial=%s result=connected duration_ms=%d",
            session.session_id,
            session.serial,
            round((time.monotonic() - started_at) * 1000),
        )
        return session.serial

    async def revoke(self, session_id: str) -> None:
        """Disconnect ADB and release the listener and accepted stream."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return

        session.revoked = True
        try:
            if session.adb_connect_attempted:
                started_at = time.monotonic()
                try:
                    await _run_adb_command("disconnect", session.serial)
                except Exception as error:
                    logger.warning(
                        "event=bridge_adb_disconnect session_id=%s serial=%s result=failed "
                        "error_type=%s duration_ms=%d",
                        session.session_id,
                        session.serial,
                        type(error).__name__,
                        round((time.monotonic() - started_at) * 1000),
                    )
                else:
                    logger.info(
                        "event=bridge_adb_disconnect session_id=%s serial=%s "
                        "result=disconnected duration_ms=%d",
                        session.session_id,
                        session.serial,
                        round((time.monotonic() - started_at) * 1000),
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
                        "event=bridge_stream_close_failed session_id=%s serial=%s error_type=%s",
                        session.session_id,
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
                        "event=bridge_listener_close_failed session_id=%s serial=%s error_type=%s",
                        session.session_id,
                        session.serial,
                        type(error).__name__,
                    )
            logger.info(
                "event=bridge_session_revoked session_id=%s serial=%s close_code=%s "
                "close_reason=%s bytes_browser_to_device=%d bytes_device_to_browser=%d",
                session.session_id,
                session.serial,
                session.close_code,
                session.close_reason,
                session.bytes_browser_to_device,
                session.bytes_device_to_browser,
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
