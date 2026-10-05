"""In-process fake adb *server* for deterministic transport tests.

Speaks the adb smart-socket protocol (``<4 hex length><payload>`` requests,
``OKAY``/``FAIL`` answers) well enough for the pinned ``adb`` binary,
``adbutils`` and ``uiautomator2`` to treat it as a real server. It owns fake
devices, so tests can run the *same serial* behind two different endpoints and
prove each call reached the endpoint it was meant for.

Never touches a real device or the machine's own adb server: it binds an
ephemeral loopback port and every client in a test must be pointed at it
explicitly (``-H/-P`` or ``AdbEndpoint``).

Supported requests (everything else answers ``FAIL``):

* host: ``version``, ``devices``, ``devices-l``, ``track-devices[-l]``,
  ``connect:``, ``disconnect:``, ``kill``, ``forward:``, ``list-forward``,
  ``transport:``, ``tport:serial:``, ``transport-id:``, ``get-state``,
  ``host-serial:<serial>:<request>``
* device services after a transport request: ``shell:``, ``shell,v2:``,
  ``exec:``, ``tcp:<port>`` (bridged to a per-device service handler)

``FakeAdbServer.requests`` records every request in order, which is the raw
material for the protocol golden files under ``tests/support/golden``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import socket
import socketserver
import struct
import threading
from typing import Any

#: Protocol version the fake reports for ``host:version``. The pinned
#: platform-tools reports 41 (adb 1.0.41); a client that sees another number
#: kills and respawns the server, which tests must never trigger.
DEFAULT_PROTOCOL_VERSION = 41

ShellHandler = Callable[["FakeDevice", str], bytes]
ServiceHandler = Callable[["FakeDevice", socket.socket], None]


def _default_shell(device: FakeDevice, command: str) -> bytes:
    if command.startswith("getprop ro.serialno"):
        return f"{device.serial}\n".encode()
    if command.startswith("echo "):
        return (command[5:] + "\n").encode()
    return f"{device.server_name}:{device.serial}:{command}\n".encode()


@dataclass
class FakeDevice:
    """One device behind a :class:`FakeAdbServer`."""

    serial: str
    state: str = "device"
    model: str = "FakePhone"
    product: str = "fake_product"
    transport_id: int = 1
    #: Name of the owning server; lets one test tell two same-serial phones apart.
    server_name: str = ""
    shell_handler: ShellHandler = _default_shell
    #: device-side TCP port -> handler owning the accepted stream.
    services: dict[int, ServiceHandler] = field(default_factory=dict)
    #: Raw bytes ``exec:screencap -p`` answers with.
    screencap: bytes = b""

    def listing(self, long_format: bool) -> str:
        if not long_format:
            return f"{self.serial}\t{self.state}"
        return (
            f"{self.serial:<22} {self.state} product:{self.product} model:{self.model} "
            f"device:{self.product} transport_id:{self.transport_id}"
        )


@dataclass(frozen=True)
class RecordedRequest:
    """One smart-socket request, with the transport selected before it."""

    connection: int
    request: str
    serial: str | None


class FakeAdbServer:
    """Threaded fake adb server bound to an ephemeral loopback port."""

    def __init__(
        self,
        name: str = "fake-adb",
        *,
        protocol_version: int = DEFAULT_PROTOCOL_VERSION,
    ) -> None:
        self.name = name
        self.protocol_version = protocol_version
        self.devices: dict[str, FakeDevice] = {}
        self.requests: list[RecordedRequest] = []
        self.forwards: list[tuple[str, str, str]] = []  # (serial, local, remote)
        #: ``host:connect:<addr>`` / ``host:disconnect:<addr>`` hooks. They run on the
        #: connection thread and return the text the server answers with, so a test
        #: can play the part of adb dialling out to an address (e.g. the device bridge).
        self.on_connect: Callable[[str], str] | None = None
        self.on_disconnect: Callable[[str], str] | None = None
        self.killed = False
        #: When set, every new connection is answered ``FAIL <message>`` (host away).
        self.offline_message: str | None = None
        self._lock = threading.Lock()
        self._next_connection = 0
        self._next_transport_id = 1
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return int(self._server.server_address[1])

    @property
    def endpoint(self) -> Any:
        """The server as an immutable :class:`artemis.runtime.adb_endpoint.AdbEndpoint`."""
        from artemis.runtime.adb_endpoint import AdbEndpoint

        return AdbEndpoint.create(self.host, self.port)

    def start(self) -> FakeAdbServer:
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                outer._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server((self.host, 0), Handler)
        self._server = server
        # The thread owns *this* server: reading ``self._server`` later would race ``stop()``,
        # which clears it, and a serve thread that died would leave ``shutdown()`` waiting forever.
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02),
            name=f"fake-adb-{self.name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        with self._lock:
            connections = list(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> FakeAdbServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # Test-facing helpers
    # ------------------------------------------------------------------ #

    def add_device(self, serial: str, **overrides: Any) -> FakeDevice:
        with self._lock:
            device = FakeDevice(
                serial=serial,
                server_name=self.name,
                transport_id=self._next_transport_id,
                **overrides,
            )
            self._next_transport_id += 1
            self.devices[serial] = device
        return device

    def remove_device(self, serial: str) -> None:
        with self._lock:
            self.devices.pop(serial, None)

    def request_strings(self, *, serial: str | None = None) -> list[str]:
        with self._lock:
            return [r.request for r in self.requests if serial is None or r.serial == serial]

    def clear_requests(self) -> None:
        with self._lock:
            self.requests.clear()

    # ------------------------------------------------------------------ #
    # Protocol
    # ------------------------------------------------------------------ #

    def _record(self, connection: int, request: str, serial: str | None) -> None:
        with self._lock:
            self.requests.append(RecordedRequest(connection, request, serial))

    @staticmethod
    def _read_exact(conn: socket.socket, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            try:
                chunk = conn.recv(size - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _read_request(self, conn: socket.socket) -> str | None:
        header = self._read_exact(conn, 4)
        if header is None:
            return None
        try:
            length = int(header.decode("ascii"), 16)
        except ValueError:
            return None
        body = self._read_exact(conn, length)
        return None if body is None else body.decode("utf-8", errors="replace")

    @staticmethod
    def _okay(conn: socket.socket, payload: str | None = None) -> None:
        if payload is None:
            conn.sendall(b"OKAY")
        else:
            data = payload.encode("utf-8")
            conn.sendall(b"OKAY" + f"{len(data):04x}".encode("ascii") + data)

    @staticmethod
    def _fail(conn: socket.socket, message: str) -> None:
        data = message.encode("utf-8")
        conn.sendall(b"FAIL" + f"{len(data):04x}".encode("ascii") + data)

    def _devices_text(self, long_format: bool) -> str:
        with self._lock:
            lines = [d.listing(long_format) for d in self.devices.values()]
        return "".join(line + "\n" for line in lines)

    def _serve(self, conn: socket.socket) -> None:
        with self._lock:
            self._next_connection += 1
            connection = self._next_connection
            self._connections.add(conn)
        try:
            self._serve_connection(conn, connection)
        except OSError:
            pass
        finally:
            with self._lock:
                self._connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _serve_connection(self, conn: socket.socket, connection: int) -> None:
        serial: str | None = None
        while True:
            request = self._read_request(conn)
            if request is None:
                return
            self._record(connection, request, serial)
            if self.offline_message is not None:
                self._fail(conn, self.offline_message)
                return

            if request == "host:version":
                self._okay(conn, f"{self.protocol_version:04x}")
                return
            if request in ("host:devices", "host:devices-l"):
                self._okay(conn, self._devices_text(request.endswith("-l")))
                return
            if request.startswith("host:track-devices"):
                self._track_devices(conn, request.endswith("-l"))
                return
            if request == "host:kill":
                self.killed = True
                self._okay(conn)
                return
            if request.startswith("host:connect:"):
                address = request[len("host:connect:") :]
                hook = self.on_connect
                self._okay(conn, hook(address) if hook else f"connected to {address}")
                return
            if request.startswith("host:disconnect:"):
                address = request[len("host:disconnect:") :]
                hook = self.on_disconnect
                self._okay(conn, hook(address) if hook else f"disconnected {address}")
                return
            if request == "host:list-forward":
                with self._lock:
                    text = "".join(f"{s} {local} {remote}\n" for s, local, remote in self.forwards)
                self._okay(conn, text)
                return
            if request.startswith("host-serial:"):
                if self._host_serial(conn, request):
                    return
                continue
            if request.startswith("host:forward:"):
                self._okay(conn)
                self._okay(conn)
                return
            if request.startswith(("host:transport:", "host:tport:serial:")):
                wanted = request.rsplit(":", 1)[1]
                with self._lock:
                    device = self.devices.get(wanted)
                if device is None:
                    self._fail(conn, f"device '{wanted}' not found")
                    return
                serial = wanted
                conn.sendall(b"OKAY")
                if request.startswith("host:tport:"):
                    conn.sendall(struct.pack("<Q", device.transport_id))
                continue
            if request.startswith("host:transport-id:"):
                wanted_id = int(request.rsplit(":", 1)[1])
                with self._lock:
                    match = next(
                        (d for d in self.devices.values() if d.transport_id == wanted_id), None
                    )
                if match is None:
                    self._fail(conn, f"device transport-id '{wanted_id}' not found")
                    return
                serial = match.serial
                conn.sendall(b"OKAY")
                continue
            if request == "host:get-state":
                self._okay(conn, "device")
                return

            if serial is None:
                self._fail(conn, "unknown host service")
                return
            with self._lock:
                device = self.devices.get(serial)
            if device is None:
                self._fail(conn, f"device '{serial}' not found")
                return
            self._device_service(conn, device, request)
            return

    def _host_serial(self, conn: socket.socket, request: str) -> bool:
        _prefix, wanted, inner = request.split(":", 2)
        with self._lock:
            device = self.devices.get(wanted)
        if device is None:
            self._fail(conn, f"device '{wanted}' not found")
            return True
        if inner == "get-state":
            self._okay(conn, device.state)
        elif inner == "get-serialno":
            self._okay(conn, device.serial)
        elif inner == "features":
            # No shell_v2: the pinned adb then falls back to plain ``shell:``.
            self._okay(conn, "cmd,stat_v2")
        elif inner.startswith("forward:") or inner.startswith("forward:norebind:"):
            parts = inner.split(";", 1)
            local = parts[0].split(":")[-1]
            remote = parts[1] if len(parts) > 1 else ""
            with self._lock:
                self.forwards.append((device.serial, local, remote))
            self._okay(conn)
            self._okay(conn)
        else:
            self._fail(conn, "unsupported host-serial request")
        return True

    def _track_devices(self, conn: socket.socket, long_format: bool) -> None:
        self._okay(conn, self._devices_text(long_format))
        # Hold the stream open (like adb) until the client goes away.
        try:
            while conn.recv(1):
                pass
        except OSError:
            pass

    def _device_service(self, conn: socket.socket, device: FakeDevice, request: str) -> None:
        if request.startswith(("shell:", "shell,v2:")):
            command = request.split(":", 1)[1]
            conn.sendall(b"OKAY")
            conn.sendall(device.shell_handler(device, command))
            return
        if request.startswith("exec:"):
            command = request.split(":", 1)[1]
            conn.sendall(b"OKAY")
            if command.startswith("screencap"):
                conn.sendall(device.screencap)
            else:
                conn.sendall(device.shell_handler(device, command))
            return
        if request.startswith("tcp:"):
            port = int(request.split(":", 1)[1])
            handler = device.services.get(port)
            if handler is None:
                self._fail(conn, f"closed (tcp:{port} on {device.serial})")
                return
            conn.sendall(b"OKAY")
            handler(device, conn)
            return
        self._fail(conn, f"unsupported service {request.split(':', 1)[0]}:")


def http_service(routes: dict[str, Callable[[str, bytes], tuple[int, bytes]]]) -> ServiceHandler:
    """Device-side HTTP/1.0 service (the accessibility helper's shape).

    ``routes`` maps a request path to ``handler(method, body) -> (status, body)``.
    The connection is closed after one response, like the helper's server.
    """

    def serve(device: FakeDevice, conn: socket.socket) -> None:
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = {
            k.strip().lower(): v.strip()
            for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)
        }
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        path = target.split("?", 1)[0]
        handler = routes.get(path)
        status, body = handler(method, rest) if handler is not None else (404, b"not found")
        reason = {200: "OK", 401: "Unauthorized", 404: "Not Found"}.get(status, "Status")
        conn.sendall(
            f"HTTP/1.0 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
            "Content-Type: application/json\r\n\r\n".encode("latin-1")
            + body
        )

    return serve
