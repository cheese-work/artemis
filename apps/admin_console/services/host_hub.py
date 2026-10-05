"""Live host-agent sockets, one per computer (last connection wins)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)

CLOSE_REPLACED = 4409
CLOSE_REVOKED = 4410


class HostHub:
    def __init__(self) -> None:
        self._conns: dict[str, tuple[WebSocket, asyncio.AbstractEventLoop, int]] = {}

    async def replace(self, host_id: str, ws: WebSocket, generation: int) -> None:
        previous = self._conns.get(host_id)
        self._conns[host_id] = (ws, asyncio.get_running_loop(), generation)
        if previous is not None:
            logger.info("event=host_connection_replaced host_id=%s", host_id)
            await self._close(previous, CLOSE_REPLACED, "replaced")

    def forget(self, host_id: str, generation: int) -> None:
        current = self._conns.get(host_id)
        if current is not None and current[2] == generation:
            del self._conns[host_id]

    async def close_host(self, host_id: str, code: int, reason: str) -> None:
        connection = self._conns.pop(host_id, None)
        if connection is not None:
            await self._close(connection, code, reason)

    @staticmethod
    async def _close(
        connection: tuple[WebSocket, asyncio.AbstractEventLoop, int], code: int, reason: str
    ) -> None:
        ws, loop, _generation = connection
        try:
            if loop is asyncio.get_running_loop():
                await ws.close(code=code, reason=reason)
            else:  # the socket's handler runs on another loop (tests, never production)
                future = asyncio.run_coroutine_threadsafe(ws.close(code=code, reason=reason), loop)
                await asyncio.wrap_future(future)
        except (RuntimeError, OSError):
            logger.debug("host socket already closed", exc_info=True)


host_hub = HostHub()
