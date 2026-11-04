"""Bounded, credit-based multiplexing; receiving never awaits a destination."""

import asyncio
from collections import deque
import struct
import time

from artemis.runtime.host_protocol import CONTRACT, MAX_PAYLOAD, Frame, FrameKind, ProtocolError


class ByteBudget:
    def __init__(self, limit: int = CONTRACT.process_budget):
        self.limit = limit
        self.used = 0
        self.waiters: set[asyncio.Event] = set()

    def reserve(self, size: int) -> bool:
        if self.used + size > self.limit:
            return False
        self.used += size
        return True

    def release(self, size: int) -> None:
        self.used -= size
        for waiter in self.waiters:
            waiter.set()


PROCESS_BUDGET = ByteBudget()


class MuxStream:
    def __init__(self, mux: "Multiplexer", stream_id: int):
        self.mux = mux
        self.stream_id = stream_id
        self.send_credit = 0
        self.in_flight = 0
        self.receive_credit = CONTRACT.stream_credit
        self.incoming = deque()
        self.outgoing = deque()
        self.acked = False
        self.remote_fin = False
        self.local_fin = False
        self.fin_sent = False
        self.reset = False
        self.closed_cleanly = False
        self.pending_credit = 0
        self.buffered = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.started_at = time.monotonic()

    async def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.reset or self.mux.closed or self.local_fin:
                raise ConnectionError("Stream closed")
            size = min(len(view), self.send_credit, MAX_PAYLOAD)
            if size and self.mux.reserve(size):
                self.outgoing.append(bytes(view[:size]))
                self.buffered += size
                self.send_credit -= size
                view = view[size:]
                self.mux.changed.set()
            else:
                self.mux.changed.clear()
                await self.mux.changed.wait()

    async def read(self, size: int) -> bytes:
        while not self.incoming:
            if self.closed_cleanly:
                return b""
            if self.reset or self.mux.closed:
                raise ConnectionError("Stream reset")
            if self.remote_fin:
                return b""
            self.mux.changed.clear()
            await self.mux.changed.wait()
        data = self.incoming.popleft()
        result, tail = data[:size], data[size:]
        if tail:
            self.incoming.appendleft(tail)
        self.buffered -= len(result)
        self.mux.release(len(result))
        self.pending_credit += len(result)
        self.mux.retire(self)
        self.mux.changed.set()
        return result

    def finish(self) -> None:
        self.local_fin = True
        self.mux.changed.set()

    def close(self) -> None:
        if not self.reset and not self.closed_cleanly:
            self.mux.control(FrameKind.RESET, self.stream_id)
            self.mux.drop(self)


class Multiplexer:
    def __init__(
        self, epoch: int, *, budget: ByteBudget | None = None, accept_remote: bool = False
    ):
        self.epoch = epoch
        self.budget = budget or PROCESS_BUDGET
        if not self.budget.reserve(CONTRACT.control_reserve):
            raise ProtocolError("Maximum process control budget exceeded")
        self.accept_remote = accept_remote
        self.streams: dict[int, MuxStream] = {}
        self.next_stream_id = 1
        self.last_remote_id = 0
        self.controls = deque()
        self.order = deque()
        self.changed = asyncio.Event()
        self.budget.waiters.add(self.changed)
        self.incoming: asyncio.Queue[MuxStream] = asyncio.Queue(CONTRACT.max_streams)
        self.buffered_bytes = 0
        self.closed = False

    def reserve(self, size: int) -> bool:
        if self.buffered_bytes + size > CONTRACT.host_budget - CONTRACT.control_reserve:
            return False
        if not self.budget.reserve(size):
            return False
        self.buffered_bytes += size
        return True

    def release(self, size: int) -> None:
        self.buffered_bytes -= size
        self.budget.release(size)
        self.changed.set()

    def control(self, kind: FrameKind, stream_id: int, payload: bytes = b"") -> None:
        if len(self.controls) >= CONTRACT.max_streams * 4:
            raise ProtocolError("Control capacity exceeded")
        self.controls.append(Frame(kind, self.epoch, stream_id, payload))
        self.changed.set()

    def _create(self, stream_id: int) -> MuxStream:
        if self.closed or len(self.streams) >= CONTRACT.max_streams:
            raise ProtocolError("Maximum streams exceeded")
        stream = MuxStream(self, stream_id)
        self.streams[stream_id] = stream
        self.order.append(stream_id)
        return stream

    def open(self) -> MuxStream:
        if self.next_stream_id >= 2**32:
            raise ProtocolError("Stream ids exhausted")
        stream = self._create(self.next_stream_id)
        self.next_stream_id += 1
        self.control(FrameKind.OPEN, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
        return stream

    def receive(self, frame: Frame) -> None:
        frame.encode()
        if frame.epoch < self.epoch:
            return
        if frame.epoch != self.epoch or self.closed:
            raise ProtocolError("Wrong connection epoch")
        if frame.kind == FrameKind.OPEN:
            if not self.accept_remote or frame.stream_id <= self.last_remote_id:
                raise ProtocolError("Unexpected OPEN")
            if self.incoming.full():
                raise ProtocolError("Maximum pending streams exceeded")
            stream = self._create(frame.stream_id)
            self.last_remote_id = frame.stream_id
            stream.send_credit = struct.unpack("!I", frame.payload)[0]
            stream.acked = True
            self.control(FrameKind.ACK, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
            self.incoming.put_nowait(stream)
            return
        stream = self.streams.get(frame.stream_id)
        if stream is None:
            highest_id = self.last_remote_id if self.accept_remote else self.next_stream_id - 1
            if frame.stream_id <= highest_id:
                return
            raise ProtocolError("Unknown stream")
        if frame.kind == FrameKind.ACK:
            if stream.acked:
                raise ProtocolError("Duplicate ACK")
            stream.acked = True
            stream.send_credit = struct.unpack("!I", frame.payload)[0]
        elif frame.kind == FrameKind.CREDIT:
            credit = struct.unpack("!I", frame.payload)[0]
            if (
                not stream.acked
                or credit > stream.in_flight
                or stream.send_credit + credit > CONTRACT.stream_credit
            ):
                raise ProtocolError("Exceeded send credit")
            stream.in_flight -= credit
            stream.send_credit += credit
        elif frame.kind == FrameKind.DATA:
            if not stream.acked or stream.remote_fin or len(frame.payload) > stream.receive_credit:
                raise ProtocolError("Exceeded receive credit")
            if not self.reserve(len(frame.payload)):
                stream.close()
                return
            stream.receive_credit -= len(frame.payload)
            stream.buffered += len(frame.payload)
            stream.bytes_received += len(frame.payload)
            stream.incoming.append(frame.payload)
        elif frame.kind == FrameKind.FIN:
            if not stream.acked or stream.remote_fin:
                raise ProtocolError("Unexpected FIN")
            stream.remote_fin = True
            self.retire(stream)
        elif frame.kind == FrameKind.RESET:
            self.drop(stream)
        self.changed.set()

    async def next_frame(self) -> Frame:
        while not self.closed:
            if self.controls:
                return self.controls.popleft()
            for _ in range(len(self.order)):
                stream_id = self.order.popleft()
                self.order.append(stream_id)
                stream = self.streams[stream_id]
                if stream.pending_credit:
                    credit, stream.pending_credit = stream.pending_credit, 0
                    stream.receive_credit += credit
                    return Frame(FrameKind.CREDIT, self.epoch, stream_id, struct.pack("!I", credit))
                if stream.outgoing:
                    data = stream.outgoing.popleft()
                    stream.in_flight += len(data)
                    stream.bytes_sent += len(data)
                    stream.buffered -= len(data)
                    self.release(len(data))
                    return Frame(FrameKind.DATA, self.epoch, stream_id, data)
                if stream.local_fin and not stream.fin_sent:
                    stream.fin_sent = True
                    self.retire(stream)
                    return Frame(FrameKind.FIN, self.epoch, stream_id)
            self.changed.clear()
            await self.changed.wait()
        raise ConnectionError("Multiplexer closed")

    def retire(self, stream: MuxStream) -> None:
        if stream.remote_fin and stream.fin_sent and not stream.incoming and not stream.outgoing:
            self.drop(stream, clean=True)

    def drop(self, stream: MuxStream, *, clean: bool = False) -> None:
        stream.reset = not clean
        stream.closed_cleanly = clean
        self.streams.pop(stream.stream_id, None)
        if stream.stream_id in self.order:
            self.order.remove(stream.stream_id)
        stream.incoming.clear()
        stream.outgoing.clear()
        self.release(stream.buffered)
        stream.buffered = 0

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for stream in list(self.streams.values()):
            self.drop(stream)
        self.controls.clear()
        self.budget.waiters.discard(self.changed)
        self.budget.release(CONTRACT.control_reserve)
        self.changed.set()
