"""B2 merge-gate regressions. Only fake computers and loopback sockets are used."""

import asyncio
import random
import struct

import pytest

from artemis.runtime.host_protocol import CONTRACT, Frame, FrameKind, ProtocolError, reconnect_delay
from artemis.runtime.host_mux import ByteBudget, Multiplexer
from artemis.runtime.adb_gateway import Gateway, filter_devices, pack_message


def test_frame_golden_vector():
    frame = Frame(FrameKind.DATA, 7, 3, b"adb")
    golden = bytes.fromhex("0300000000000000070000000300000003") + b"adb"
    assert frame.encode() == golden
    assert Frame.decode(golden) == frame


@pytest.mark.parametrize("kind", list(FrameKind))
def test_frame_roundtrip(kind):
    payload = (
        struct.pack("!I", CONTRACT.stream_credit)
        if kind in {FrameKind.OPEN, FrameKind.ACK, FrameKind.CREDIT}
        else b"data"
        if kind == FrameKind.DATA
        else b""
    )
    frame = Frame(kind, 1, 1, payload)
    assert Frame.decode(frame.encode()) == frame


def test_codec_fuzz_is_bounded_and_rejects_bad_lengths():
    random_source = random.Random(1096)
    for _ in range(1000):
        payload = random_source.randbytes(random_source.randrange(150))
        try:
            frame = Frame.decode(payload)
        except ProtocolError:
            continue
        assert len(frame.encode()) <= CONTRACT.max_frame
    with pytest.raises(ProtocolError):
        Frame(FrameKind.DATA, 1, 1, bytes(CONTRACT.max_frame)).encode()
    with pytest.raises(ProtocolError):
        Frame.decode(bytes.fromhex("030000000000000001000000010000ffff"))


@pytest.mark.asyncio
async def test_mux_stream_cap_credit_violation_and_stale_epoch():
    mux = Multiplexer(7)
    streams = [mux.open() for _ in range(CONTRACT.max_streams)]
    with pytest.raises(ProtocolError, match="streams"):
        mux.open()
    mux.receive(Frame(FrameKind.DATA, 6, streams[0].stream_id, b"old"))
    assert mux.buffered_bytes == 0
    with pytest.raises(ProtocolError, match="credit"):
        mux.receive(Frame(FrameKind.DATA, 7, streams[0].stream_id, b"x"))
    mux.close()
    assert mux.budget.used == 0


@pytest.mark.asyncio
async def test_mux_stream_id_exhaustion():
    mux = Multiplexer(1)
    mux.next_stream_id = 2**32
    with pytest.raises(ProtocolError, match="exhausted"):
        mux.open()
    mux.close()


@pytest.mark.asyncio
async def test_mux_blocked_destination_never_stalls_control_or_other_stream():
    mux = Multiplexer(1)
    slow, fast = mux.open(), mux.open()
    await mux.next_frame()
    await mux.next_frame()
    for stream in (slow, fast):
        mux.receive(
            Frame(FrameKind.ACK, 1, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
        )
    mux.receive(Frame(FrameKind.DATA, 1, slow.stream_id, b"blocked"))
    mux.receive(Frame(FrameKind.DATA, 1, fast.stream_id, b"ready"))
    assert await fast.read(5) == b"ready"
    assert (await mux.next_frame()).kind == FrameKind.CREDIT
    await fast.write(b"reply")
    assert (await mux.next_frame()).payload == b"reply"
    assert mux.buffered_bytes == len(b"blocked")
    mux.close()


@pytest.mark.asyncio
async def test_mux_fairness_fin_order_and_budget_release():
    budget = ByteBudget(CONTRACT.host_budget * 2)
    mux = Multiplexer(1, budget=budget)
    first, second = mux.open(), mux.open()
    await mux.next_frame()
    await mux.next_frame()
    for stream in (first, second):
        mux.receive(
            Frame(FrameKind.ACK, 1, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
        )
        await stream.write(b"x" * 100)
        await stream.write(b"y" * 100)
    first.finish()
    frames = [await mux.next_frame() for _ in range(4)]
    assert [frame.stream_id for frame in frames] == [first.stream_id, second.stream_id] * 2
    assert (await mux.next_frame()).kind == FrameKind.FIN
    mux.close()
    assert budget.used == 0


@pytest.mark.asyncio
async def test_mux_fuzz_window_and_saturation():
    mux = Multiplexer(3, budget=ByteBudget(CONTRACT.host_budget))
    random_source = random.Random(32)
    streams = [mux.open() for _ in range(32)]
    for stream in streams:
        mux.receive(
            Frame(FrameKind.ACK, 3, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
        )
    for _ in range(2000):
        stream = random_source.choice(streams)
        payload = random_source.randbytes(random_source.randrange(1, 4096))
        try:
            mux.receive(Frame(FrameKind.DATA, 3, stream.stream_id, payload))
        except ProtocolError:
            break
        assert mux.buffered_bytes <= CONTRACT.host_budget - CONTRACT.control_reserve
        assert mux.budget.used <= mux.budget.limit
    mux.close()
    assert mux.budget.used == 0


def test_devices_rewrite_and_utf8_limits():
    payload = b"private\tdevice\nshared\tdevice model:Phone\n"
    assert filter_devices(payload, {"shared"}) == b"shared\tdevice model:Phone\n"
    for bad in (b"\xff", b"x" * (CONTRACT.max_text + 1), b"shared\tdevice\x00\n"):
        with pytest.raises(ProtocolError):
            filter_devices(bad, {"shared"})


class FakeRemote:
    def __init__(self):
        self.sent = []
        self.replies = asyncio.StreamReader()

    async def write(self, data):
        self.sent.append(data)
        service = data[4:].decode()
        self.replies.feed_data(b"OKAY")
        if service == "host:version":
            self.replies.feed_data(pack_message(b"0029"))
        elif service == "host:features" or service.endswith(":features"):
            self.replies.feed_data(pack_message(b"shell_v2,cmd,stat_v2"))
        elif service in {"host:devices", "host:devices-l"}:
            self.replies.feed_data(pack_message(b"hidden\tdevice\nphone\tdevice\n"))
        elif service.startswith("host:tport:serial:"):
            self.replies.feed_data(struct.pack("<Q", 0x0102030405060708))
        elif service.startswith("host-serial:"):
            if service.rpartition(":")[2].startswith("wait-for-"):
                self.replies.feed_data(b"OKAY")
            elif service.endswith(":get-state"):
                self.replies.feed_data(pack_message(b"device"))
            elif service.endswith(":get-serialno"):
                self.replies.feed_data(pack_message(service[12:].rsplit(":", 1)[0].encode()))
            self.replies.feed_eof()
        elif not service.startswith("host:transport:"):
            self.replies.feed_data(b"output")
            self.replies.feed_eof()

    async def read(self, size):
        return await self.replies.read(size)


class FakeWriter:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass


async def gateway_exchange(commands, *, fragmented=False, shared=None):
    reader = asyncio.StreamReader()
    writer, remote = FakeWriter(), FakeRemote()
    gateway = Gateway(lambda: {"phone"} if shared is None else shared)
    payload = b"".join(pack_message(command.encode()) for command in commands)
    if fragmented:
        for byte in payload:
            reader.feed_data(bytes([byte]))
    else:
        reader.feed_data(payload)
    reader.feed_eof()
    await gateway.relay(reader, writer, remote)
    return writer, remote, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service",
    [
        "host:kill",
        "host:connect:evil",
        "host:disconnect:phone",
        "host:forward:tcp:1;tcp:2",
        "host:killforward-all",
        "host:pair:evil",
        "host:mdns:services",
        "host:emulator:kill",
        "host:transport:hidden",
        "host:transport-any",
        "host:tport:serial:hidden",
        "host:tport:serial:phone\n",
        "host:tport:any",
        "host:tport:usb",
        "host:tport:local",
        "host:tport:serial:phone:extra",
        "host-serial:hidden:features",
        "host-serial:hidden:get-state",
        "host-serial:hidden:get-serialno",
        "host-serial:hidden:wait-for-any-device",
        "host-serial:phone:forward:tcp:1;tcp:2",
        "host-serial:phone:forward:norebind:tcp:1;tcp:2",
        "host-serial:phone:killforward-all",
        "host-serial:phone:killforward:tcp:1",
        "host-serial:phone:root:",
        "host-serial:phone:get-devpath",
        "host-serial:phone:unknown",
        "host-serial:phone:features:extra",
        "host-serial:phone:wait-for-any-device:forward:tcp:1;tcp:2",
        "host-serial:phone:wait-for-",
        "host-serial:phone:wait-for-evil-device",
        "host:wait-for-any-device",
        "host:features:extra",
        "shell,v2,raw:id",
        "host:unknown",
        "shell:id",
    ],
)
async def test_gateway_allowlist_before_transport(service, caplog):
    writer, remote, _ = await gateway_exchange([service])
    assert bytes(writer.data).startswith(b"FAIL")
    assert remote.sent == []
    assert "event=adb_denied" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service",
    [
        "reverse:forward:tcp:1;tcp:2",
        "root:",
        "unroot:",
        "remount:",
        "tcpip:5555",
        "usb:",
        "host:kill",
        "host:forward:tcp:1;tcp:2",
        "host:transport:hidden",
        "host:tport:serial:hidden",
        "host:tport:serial:phone",
        "host-serial:phone:features",
        "host-serial:phone:forward:tcp:1;tcp:2",
        "sync",
        "shell",
        "unknown:",
    ],
)
async def test_gateway_deny_after_transport(service):
    writer, remote, _ = await gateway_exchange(["host:transport:phone", service])
    assert bytes(writer.data).startswith(b"OKAYFAIL")
    assert remote.sent == [pack_message(b"host:transport:phone")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service", ["shell:id", "exec:cat", "sync:", "tcp:9008", "localabstract:scrcpy", "framebuffer:"]
)
@pytest.mark.parametrize("fragmented", [True, False])
async def test_gateway_nested_transport_fragmented_and_coalesced(service, fragmented):
    writer, remote, gateway = await gateway_exchange(
        ["host:transport:phone", service], fragmented=fragmented
    )
    assert bytes(writer.data) == b"OKAYOKAYoutput"
    assert gateway.serial == "phone"
    assert len(remote.sent) == 2


@pytest.mark.asyncio
async def test_gateway_devices_filter():
    writer, _, _ = await gateway_exchange(["host:devices-l"])
    assert bytes(writer.data) == b"OKAY" + pack_message(b"phone\tdevice\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("serial", ["phone", "127.0.0.1:5555"])
@pytest.mark.parametrize("fragmented", [False, True])
@pytest.mark.parametrize(
    "operation", ["features", "host_features", "shell", "wait", "state", "serial", "exec", "sync"]
)
async def test_gateway_adb_1_0_41_conformance(serial, fragmented, operation):
    exchanges = {
        "features": (
            [f"host-serial:{serial}:features"],
            b"OKAY" + pack_message(b"shell_v2,cmd,stat_v2"),
        ),
        "host_features": (["host:features"], b"OKAY" + pack_message(b"shell_v2,cmd,stat_v2")),
        "shell": (
            [f"host:tport:serial:{serial}", "shell,v2,TERM=xterm-256color,raw:id"],
            b"OKAY" + struct.pack("<Q", 0x0102030405060708) + b"OKAYoutput",
        ),
        "wait": ([f"host-serial:{serial}:wait-for-any-device"], b"OKAYOKAY"),
        "state": ([f"host-serial:{serial}:get-state"], b"OKAY" + pack_message(b"device")),
        "serial": ([f"host-serial:{serial}:get-serialno"], b"OKAY" + pack_message(serial.encode())),
        "exec": (
            [f"host:tport:serial:{serial}", "exec:cat"],
            b"OKAY" + struct.pack("<Q", 0x0102030405060708) + b"OKAYoutput",
        ),
        "sync": (
            [f"host:tport:serial:{serial}", "sync:"],
            b"OKAY" + struct.pack("<Q", 0x0102030405060708) + b"OKAYoutput",
        ),
    }
    commands, expected = exchanges[operation]
    writer, remote, gateway = await asyncio.wait_for(
        gateway_exchange(commands, fragmented=fragmented, shared={serial}), 1
    )
    assert bytes(writer.data) == expected
    assert remote.sent == [pack_message(command.encode()) for command in commands]
    if commands[0] != "host:features":
        assert gateway.serial == serial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "features",
        "get-state",
        "get-serialno",
        "wait-for-usb-recovery",
        "wait-for-local-disconnect",
        "wait-for-any-device-recovery",
    ],
)
async def test_gateway_scoped_serial_with_wait_substring(command):
    serial = "phone:wait-for-any-device"
    writer, remote, gateway = await gateway_exchange(
        [f"host-serial:{serial}:{command}"], shared={serial}
    )
    payloads = {
        "features": b"shell_v2,cmd,stat_v2",
        "get-state": b"device",
        "get-serialno": serial.encode(),
    }
    expected = (
        b"OKAYOKAY"
        if command.startswith("wait-for-")
        else b"OKAY" + pack_message(payloads[command])
    )
    assert bytes(writer.data) == expected
    assert gateway.serial == serial
    assert len(remote.sent) == 1


@pytest.mark.asyncio
async def test_gateway_parser_fuzz_never_forwards_unknown_input():
    random_source = random.Random(1096)
    for _ in range(100):
        reader = asyncio.StreamReader()
        reader.feed_data(random_source.randbytes(random_source.randrange(1, 100)))
        reader.feed_eof()
        remote = FakeRemote()
        await Gateway(lambda: {"phone"}).relay(reader, FakeWriter(), remote)
        assert remote.sent == []


def test_reconnect_schedule_jitter_and_shared_constants():
    assert CONTRACT.ping_seconds == 20
    assert CONTRACT.dead_seconds == 50
    assert CONTRACT.grace_seconds == 30
    assert reconnect_delay(0, active=True, random_value=0.5) == 0.5
    assert reconnect_delay(30, active=True, random_value=1) <= 5
    assert reconnect_delay(30, active=False, random_value=1) <= 30


def test_protocol_doc_matches_shared_table():
    from pathlib import Path
    from artemis.runtime.host_protocol import protocol_document

    assert Path("docs/host-tunnel-protocol.md").read_text() == protocol_document()


@pytest.mark.asyncio
async def test_host_peer_and_server_use_real_fake_adb_wire_and_half_close():
    from artemis.runtime.host_peer import HostPeer

    async def fake_adb(reader, writer):
        try:
            prefix = await reader.readexactly(4)
            request = await reader.readexactly(int(prefix, 16))
            assert request == b"host:devices"
            writer.write(b"OKAY" + pack_message(b"private\tdevice\nphone\tdevice\n"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(fake_adb, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    peer = HostPeer(
        1, lambda: {"phone"}, connector=lambda: asyncio.open_connection("127.0.0.1", port)
    )
    server = Multiplexer(1)

    async def to_host():
        while True:
            peer.receive((await server.next_frame()).encode())

    async def to_server():
        while True:
            server.receive(await peer.mux.next_frame())

    pumps = [asyncio.create_task(to_host()), asyncio.create_task(to_server())]
    try:
        stream = server.open()
        await asyncio.wait_for(stream.write(pack_message(b"host:devices")), 1)
        response = bytearray()
        while data := await asyncio.wait_for(stream.read(65519), 1):
            response.extend(data)
        assert bytes(response) == b"OKAY" + pack_message(b"phone\tdevice\n")
        stream.finish()
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        server.close()
        await peer.close()
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
async def test_credit_must_not_exceed_bytes_actually_sent():
    mux = Multiplexer(1)
    stream = mux.open()
    await mux.next_frame()
    mux.receive(
        Frame(FrameKind.ACK, 1, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
    )
    await stream.write(b"not sent yet")
    with pytest.raises(ProtocolError, match="credit"):
        mux.receive(Frame(FrameKind.CREDIT, 1, stream.stream_id, struct.pack("!I", 1)))
    mux.close()


@pytest.mark.asyncio
async def test_process_cap_reserves_control_capacity_and_releases_it():
    budget = ByteBudget(CONTRACT.control_reserve)
    first = Multiplexer(1, budget=budget)
    with pytest.raises(ProtocolError, match="process"):
        Multiplexer(2, budget=budget)
    first.close()
    assert budget.used == 0


@pytest.mark.asyncio
async def test_receive_saturation_resets_only_overflowing_stream():
    budget = ByteBudget(CONTRACT.control_reserve + 8)
    mux = Multiplexer(1, budget=budget)
    blocked, healthy = mux.open(), mux.open()
    await mux.next_frame()
    await mux.next_frame()
    for stream in (blocked, healthy):
        mux.receive(
            Frame(FrameKind.ACK, 1, stream.stream_id, struct.pack("!I", CONTRACT.stream_credit))
        )
    mux.receive(Frame(FrameKind.DATA, 1, blocked.stream_id, b"12345678"))
    mux.receive(Frame(FrameKind.DATA, 1, healthy.stream_id, b"overflow"))
    reset = await mux.next_frame()
    assert reset.kind == FrameKind.RESET
    assert reset.stream_id == healthy.stream_id
    assert blocked.stream_id in mux.streams
    assert not mux.closed
    assert budget.used <= budget.limit
    assert await blocked.read(8) == b"12345678"
    mux.close()
    assert budget.used == 0


@pytest.mark.asyncio
async def test_fin_releases_stream_slots_without_reusing_ids():
    mux = Multiplexer(1)
    for stream_id in range(1, 65):
        stream = mux.open()
        assert stream.stream_id == stream_id
        await mux.next_frame()
        mux.receive(Frame(FrameKind.ACK, 1, stream_id, struct.pack("!I", CONTRACT.stream_credit)))
        mux.receive(Frame(FrameKind.FIN, 1, stream_id))
        stream.finish()
        assert (await mux.next_frame()).kind == FrameKind.FIN
        assert await stream.read(1) == b""
        assert not mux.streams
    mux.close()


@pytest.mark.asyncio
async def test_failed_transport_never_forwards_coalesced_service():
    remote = FakeRemote()

    async def reject(data):
        remote.sent.append(data)
        remote.replies.feed_data(b"FAIL" + pack_message(b"device offline"))

    remote.write = reject
    reader = asyncio.StreamReader()
    reader.feed_data(pack_message(b"host:transport:phone") + pack_message(b"shell:id"))
    reader.feed_eof()
    writer = FakeWriter()
    await Gateway(lambda: {"phone"}).relay(reader, writer, remote)
    assert remote.sent == [pack_message(b"host:transport:phone")]
    assert bytes(writer.data) == b"FAIL" + pack_message(b"device offline")


@pytest.mark.asyncio
async def test_track_devices_rechecks_share_set_for_every_update():
    shared = {"phone"}
    remote = FakeRemote()
    updates = asyncio.StreamReader()
    updates.feed_data(b"OKAY" + pack_message(b"phone\tdevice\n") * 2)
    updates.feed_eof()
    remote.replies = updates

    async def write_request(data):
        remote.sent.append(data)

    remote.write = write_request
    reader = asyncio.StreamReader()
    reader.feed_data(pack_message(b"host:track-devices"))
    writer = FakeWriter()
    drains = 0

    async def drain():
        nonlocal drains
        drains += 1
        if drains == 2:
            shared.clear()

    writer.drain = drain
    await Gateway(lambda: shared).relay(reader, writer, remote)
    assert bytes(writer.data).startswith(b"OKAY" + pack_message(b"phone\tdevice\n") + b"0000")
