"""Non-overridable smart-socket allowlist, shared by the host and server."""

import asyncio
from collections.abc import Callable
import logging
import re

from artemis.runtime.host_protocol import CONTRACT, MAX_PAYLOAD, ProtocolError

logger = logging.getLogger(__name__)
FRAME_SECONDS = 5
_HEX = re.compile(rb"[0-9a-fA-F]{4}\Z")
_SERIAL = re.compile(r"[A-Za-z0-9._:\-]{1,64}\Z")
_HOST_SERVICES = frozenset(
    {
        "host:version",
        "host:features",
        "host:devices",
        "host:devices-l",
        "host:track-devices",
        "host:track-devices-l",
    }
)
_TRANSPORT_PREFIXES = ("host:transport:", "host:tport:serial:")
_SERIAL_SERVICES = frozenset({"features", "get-state", "get-serialno"})
_WAIT_SERVICE = re.compile(
    r"wait-for-(?:any|usb|local)-(?:device|recovery|rescue|sideload|bootloader|any|disconnect)"
    r"(?:-(?:device|recovery|rescue|sideload|bootloader|any|disconnect))*\Z"
)
_DEVICE_SERVICES = (
    "shell:",
    "shell,",
    "exec:",
    "abb_exec:",
    "sync:",
    "tcp:",
    "localabstract:",
    "framebuffer:",
)


def pack_message(payload: bytes) -> bytes:
    if not 0 < len(payload) <= CONTRACT.max_text:
        if not payload:
            return b"0000"
        raise ProtocolError("ADB text exceeds limit")
    return f"{len(payload):04x}".encode() + payload


def text(payload: bytes, *, allow_nul: bool = False) -> str:
    if len(payload) > CONTRACT.max_text:
        raise ProtocolError("ADB text exceeds limit")
    try:
        result = payload.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ProtocolError("Invalid UTF-8") from error
    if any(
        ord(character) < 32 and character not in "\t\n\r" and not (allow_nul and character == "\0")
        for character in result
    ):
        raise ProtocolError("Invalid text control character")
    return result


def filter_devices(payload: bytes, shared: set[str]) -> bytes:
    result = []
    for line in text(payload).splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) < 2 or not _SERIAL.fullmatch(fields[0]):
            raise ProtocolError("Malformed devices list")
        if fields[0] in shared:
            result.append(line + "\n")
    return "".join(result).encode()


async def read_exact(source, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = await source.read(size - len(data))
        if not part:
            raise asyncio.IncompleteReadError(bytes(data), size)
        data.extend(part)
    return bytes(data)


async def read_message(source, *, timeout: float | None = None) -> bytes:
    # Idle connections are owned by the tunnel; a partial frame has one deadline.
    first = await read_exact(source, 1)

    async def remainder():
        prefix = first + await read_exact(source, 3)
        if not _HEX.fullmatch(prefix):
            raise ProtocolError("Malformed ADB length")
        return await read_exact(source, int(prefix, 16))

    if timeout is None:
        return await remainder()
    return await asyncio.wait_for(remainder(), timeout)


class Gateway:
    def __init__(self, shared: Callable[[], set[str]], *, r1=False, authorize=None):
        self.shared = shared
        self.serial: str | None = None
        self.r1 = r1
        self.authorize = authorize

    def check(self):
        if self.authorize is not None and self.authorize() is not True:
            raise ProtocolError("Access ended")

    async def reply_bytes(self, remote, size, *, waiting=False):
        if not self.r1:
            return await read_exact(remote, size)
        if waiting:
            first = await read_exact(remote, 1)  # the owned wait may last until lease expiry
            return first + await asyncio.wait_for(read_exact(remote, size - 1), FRAME_SECONDS)
        return await asyncio.wait_for(read_exact(remote, size), FRAME_SECONDS)

    def allows(self, service: str) -> bool:
        self.check()
        if "\0" in service and not service.startswith("abb_exec:"):
            return False
        if self.serial is not None:
            if self.r1 and service.startswith(("tcp:", "localabstract:")):
                return False
            return self.serial in self.shared() and service.startswith(_DEVICE_SERVICES)
        if service in _HOST_SERVICES:
            return True
        for prefix in _TRANSPORT_PREFIXES:
            if service.startswith(prefix):
                serial = service[len(prefix) :]
                return bool(_SERIAL.fullmatch(serial) and serial in self.shared())
        if service.startswith("host-serial:"):
            serial, _, command = service[len("host-serial:") :].rpartition(":")
            return bool(
                _SERIAL.fullmatch(serial)
                and serial in self.shared()
                and (command in _SERIAL_SERVICES or _WAIT_SERVICE.fullmatch(command))
            )
        return False

    async def relay(self, reader, writer, remote) -> None:
        try:
            while True:
                request = await read_message(reader, timeout=FRAME_SECONDS if self.r1 else None)
                service = text(
                    request,
                    allow_nul=bool(
                        self.serial is not None
                        and self.serial in self.shared()
                        and request.startswith(b"abb_exec:")
                    ),
                )
                if self.r1 and any(
                    ord(c) < 32
                    and not (c == "\0" and self.serial and service.startswith("abb_exec:"))
                    for c in service
                ):
                    raise ProtocolError("Invalid request control character")
                if not self.allows(service):
                    # Never log shell arguments, paths, unknown families or parser payloads.
                    family = service.split(":", 1)[0].split(",", 1)[0]
                    if family not in {"host", "host-serial", "shell", "exec", "sync", "abb_exec"}:
                        family = "other"
                    logger.warning("event=adb_denied family=%s", family)
                    writer.write(b"FAIL" + pack_message(b"Service is not allowed"))
                    await writer.drain()
                    return
                if self.r1 and (
                    service == "host:features"
                    or (service.startswith("host-serial:") and service.endswith(":features"))
                ):
                    writer.write(b"OKAY0000")
                    await writer.drain()
                    return
                if service.startswith("host-serial:"):
                    self.serial = service[len("host-serial:") :].rpartition(":")[0]
                self.check()
                await remote.write(pack_message(request))
                status = await self.reply_bytes(remote, 4)
                if status == b"FAIL":
                    message = await read_message(remote, timeout=FRAME_SECONDS if self.r1 else None)
                    text(message)
                    writer.write(status + pack_message(message))
                    await writer.drain()
                    return
                if status != b"OKAY":
                    raise ProtocolError("Invalid ADB status")
                writer.write(status)
                await writer.drain()
                if service.startswith(_TRANSPORT_PREFIXES):
                    prefix = next(
                        prefix for prefix in _TRANSPORT_PREFIXES if service.startswith(prefix)
                    )
                    self.serial = service[len(prefix) :]
                    if prefix == "host:tport:serial:":
                        writer.write(await self.reply_bytes(remote, 8))
                        await writer.drain()
                    continue
                if service in _HOST_SERVICES or service.startswith("host-serial:"):
                    if service.startswith("host-serial:") and service.rpartition(":")[2].startswith(
                        "wait-for-"
                    ):
                        status = await self.reply_bytes(remote, 4, waiting=True)
                        if status == b"OKAY":
                            writer.write(status)
                        elif status == b"FAIL":
                            message = await read_message(
                                remote, timeout=FRAME_SECONDS if self.r1 else None
                            )
                            text(message)
                            writer.write(status + pack_message(message))
                        else:
                            raise ProtocolError("Invalid ADB wait status")
                        await writer.drain()
                        return
                    while True:
                        response = await read_message(
                            remote, timeout=FRAME_SECONDS if self.r1 else None
                        )
                        self.check()
                        if service in {
                            "host:devices",
                            "host:devices-l",
                            "host:track-devices",
                            "host:track-devices-l",
                        }:
                            response = filter_devices(response, self.shared())
                        else:
                            text(response)
                        writer.write(pack_message(response))
                        await writer.drain()
                        if not service.startswith("host:track-devices"):
                            return

                async def upstream():
                    while data := await reader.read(MAX_PAYLOAD):
                        self.check()
                        await remote.write(data)
                    remote.finish()

                async def downstream():
                    while data := await remote.read(MAX_PAYLOAD):
                        self.check()
                        writer.write(data)
                        await writer.drain()

                send = asyncio.create_task(upstream())
                try:
                    await downstream()
                finally:
                    send.cancel()
                    await asyncio.gather(send, return_exceptions=True)
                return
        except (asyncio.IncompleteReadError, ProtocolError, UnicodeError, TimeoutError):
            writer.write(b"FAIL" + pack_message(b"Invalid ADB request or response"))
            await writer.drain()
