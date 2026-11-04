"""Host-side B2 primitive for the B3 agent; no host installer or CLI here."""

import asyncio
from collections.abc import Callable

from artemis.runtime.adb_gateway import Gateway
from artemis.runtime.host_mux import Multiplexer
from artemis.runtime.host_protocol import CONTRACT, Frame


class SocketRemote:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def write(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def read(self, size):
        return await self.reader.read(size)

    def finish(self):
        if self.writer.can_write_eof():
            self.writer.write_eof()


class StreamWriter:
    def __init__(self, stream):
        self.stream = stream
        self.pending = bytearray()

    def write(self, data):
        self.pending.extend(data)

    async def drain(self):
        await self.stream.write(bytes(self.pending))
        self.pending.clear()


async def connect_adb():
    return await asyncio.open_connection("127.0.0.1", 5037)


class HostPeer:
    def __init__(self, epoch: int, shared: Callable[[], set[str]], *, connector=connect_adb):
        self.mux = Multiplexer(epoch, accept_remote=True)
        self.shared = shared
        self.connector = connector
        self.tasks: set[asyncio.Task] = set()
        self.gateways: dict[int, Gateway] = {}

    def receive(self, payload: bytes):
        self.mux.receive(Frame.decode(payload))
        while not self.mux.incoming.empty():
            stream = self.mux.incoming.get_nowait()
            task = asyncio.create_task(self._relay(stream))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _relay(self, stream):
        writer = None
        gateway = Gateway(self.shared)
        self.gateways[stream.stream_id] = gateway
        try:
            reader, writer = await asyncio.wait_for(self.connector(), CONTRACT.dead_seconds)
            await gateway.relay(stream, StreamWriter(stream), SocketRemote(reader, writer))
            stream.finish()
        except (OSError, ConnectionError, ValueError, TimeoutError):
            stream.close()
        finally:
            self.gateways.pop(stream.stream_id, None)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, ConnectionError):
                    pass

    def unshare(self):
        shared = self.shared()
        for stream_id, gateway in self.gateways.items():
            if gateway.serial is not None and gateway.serial not in shared:
                stream = self.mux.streams.get(stream_id)
                if stream is not None:
                    stream.close()

    async def close(self):
        self.mux.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
