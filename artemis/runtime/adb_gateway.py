"""Non-overridable smart-socket allowlist, shared by the host and server."""

import asyncio
from collections.abc import Callable
import logging
import re

from artemis.runtime.host_protocol import CONTRACT, MAX_PAYLOAD, ProtocolError

logger = logging.getLogger(__name__)
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


async def read_message(source) -> bytes:
    prefix = await read_exact(source, 4)
    if not _HEX.fullmatch(prefix):
        raise ProtocolError("Malformed ADB length")
    return await read_exact(source, int(prefix, 16))


class Gateway:
    def __init__(self, shared: Callable[[], set[str]]):
        self.shared = shared
        self.serial: str | None = None

    def allows(self, service: str) -> bool:
        if "\0" in service and not service.startswith("abb_exec:"):
            return False
        if self.serial is not None:
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
                request = await read_message(reader)
                service = text(
                    request,
                    allow_nul=bool(
                        self.serial is not None
                        and self.serial in self.shared()
                        and request.startswith(b"abb_exec:")
                    ),
                )
                if not self.allows(service):
                    logger.warning("event=adb_denied service=%r", service[:128])
                    writer.write(b"FAIL" + pack_message(b"Service is not allowed"))
                    await writer.drain()
                    return
                if service.startswith("host-serial:"):
                    self.serial = service[len("host-serial:") :].rpartition(":")[0]
                await remote.write(pack_message(request))
                status = await read_exact(remote, 4)
                if status == b"FAIL":
                    message = await read_message(remote)
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
                        writer.write(await read_exact(remote, 8))
                        await writer.drain()
                    continue
                if service in _HOST_SERVICES or service.startswith("host-serial:"):
                    if service.startswith("host-serial:") and service.rpartition(":")[2].startswith(
                        "wait-for-"
                    ):
                        status = await read_exact(remote, 4)
                        if status == b"OKAY":
                            writer.write(status)
                        elif status == b"FAIL":
                            message = await read_message(remote)
                            text(message)
                            writer.write(status + pack_message(message))
                        else:
                            raise ProtocolError("Invalid ADB wait status")
                        await writer.drain()
                        return
                    while True:
                        response = await read_message(remote)
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
                        await remote.write(data)
                    remote.finish()

                async def downstream():
                    while data := await remote.read(MAX_PAYLOAD):
                        writer.write(data)
                        await writer.drain()

                send = asyncio.create_task(upstream())
                try:
                    await downstream()
                finally:
                    send.cancel()
                    await asyncio.gather(send, return_exceptions=True)
                return
        except (asyncio.IncompleteReadError, ProtocolError, UnicodeError):
            writer.write(b"FAIL" + pack_message(b"Invalid ADB request or response"))
            await writer.drain()
