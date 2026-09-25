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

"""Session-lease bookkeeping for the browser device-bridge WSS stub.

This is only the security/lifecycle substrate for a future browser-side
WebUSB/ADB bridge (CHE-481, CHE-784). It does not parse or emulate ADB, does
not enumerate a device, and never invokes ``adb`` — it leases an opaque,
bounded-lifetime session and an ephemeral loopback TCP listener that a later
protocol client would attach to. The listener here never accepts real ADB
traffic; it exists so the lease/cleanup contract can be exercised end to end.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
import socket
import time
import uuid

DEFAULT_SESSION_TTL_SECONDS = 300


def _session_ttl_seconds() -> float:
    raw = os.environ.get("ARTEMIS_BRIDGE_SESSION_TTL_SECONDS")
    if not raw:
        return DEFAULT_SESSION_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError:
        return DEFAULT_SESSION_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_SESSION_TTL_SECONDS


@dataclass
class BridgeSession:
    """One leased, loopback-only listener bound to a single WSS connection."""

    session_id: str
    listener: socket.socket
    port: int
    created_at: float = field(default_factory=time.monotonic)
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def remaining_seconds(self) -> float:
        """Seconds left in this lease's lifetime, floored at 0."""
        return max(0.0, self.expires_at - time.monotonic())


class BridgeSessionService:
    """Authorizes and tracks device-bridge session leases.

    One listener per session: a session is created, bound, and torn down as a
    unit. Cleanup is idempotent so it can safely run from close, disconnect,
    and timeout paths without double-closing a socket.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BridgeSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self) -> BridgeSession:
        """Bind a fresh loopback listener and register its lease.

        Binds on port 0 so the OS picks a free ephemeral port; the socket is
        never exposed beyond 127.0.0.1.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
        except Exception:
            listener.close()
            raise
        listener.setblocking(False)

        session = BridgeSession(
            session_id=uuid.uuid4().hex,
            listener=listener,
            port=listener.getsockname()[1],
        )
        session.expires_at = time.monotonic() + _session_ttl_seconds()

        async with self._lock:
            self._sessions[session.session_id] = session
        return session

    async def revoke(self, session_id: str) -> None:
        """Tear down and forget a session. Safe to call more than once."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            _close_listener(session.listener)

    async def get(self, session_id: str) -> BridgeSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def active_session_ids(self) -> set[str]:
        async with self._lock:
            return set(self._sessions.keys())


def _close_listener(listener: socket.socket) -> None:
    try:
        listener.close()
    except OSError:
        pass


# Process-wide instance, mirroring the other admin_console services.
bridge_session_service = BridgeSessionService()
