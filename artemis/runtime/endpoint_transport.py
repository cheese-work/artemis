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

"""The one place that talks to an adb server on a run's behalf.

Every adb subprocess, adbutils client and uiautomator2 connection is built here
from an explicit :class:`~artemis.runtime.adb_endpoint.AdbEndpoint`, so a run
bound to one adb server cannot silently reach another (the machine's own server,
or a different host's tunnel). ``tests/unit/runtime/test_endpoint_transport_lint.py``
bans bare adb access elsewhere; the allowlist there only shrinks.

Two behaviours differ from calling adb or adbutils directly, both for endpoints
that are not the local default server:

* **Never spawn a server for someone else's endpoint.** adbutils runs a *local*
  ``adb start-server`` whenever its connection fails, whatever host it was
  given (a remote hostname included), and the adb client does the same for a
  refused loopback port. For a tunnel or a forwarded port that would start a
  stray server and hide the real problem. adbutils clients are therefore built
  without the spawn for every endpoint but the local default, and the adb
  subprocess path refuses a refused loopback port with
  :class:`EndpointUnreachable`.
* **Local-only operations are refused.** ``start-server``, ``kill-server`` and
  key healing act on this machine's server. :meth:`EndpointTransport.require_local`
  raises :class:`LocalOnlyOperation` rather than touching it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import socket
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from artemis.runtime.adb_endpoint import AdbEndpoint, AdbSession, current_adb_endpoint

if TYPE_CHECKING:
    from adbutils import AdbClient, AdbDevice

DEFAULT_PROBE_TIMEOUT_SECONDS = 1.5
_SHARED_LIMIT = 64
_SHARED: dict[AdbEndpoint, EndpointTransport] = {}
_SHARED_LOCK = threading.Lock()


class EndpointUnreachable(OSError):
    """The adb server of an endpoint cannot be reached (for a host: it is offline)."""


class LocalOnlyOperation(RuntimeError):
    """A local-server operation was attempted against a non-local endpoint."""


class EndpointTransport:
    """adb access bound to one immutable endpoint."""

    def __init__(
        self,
        endpoint: AdbEndpoint,
        adb_path: str | None = None,
        *,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint
        self._adb_path = adb_path
        self._probe_timeout = probe_timeout
        self._client: AdbClient | None = None
        self._streams_lock = threading.Lock()
        self._open_streams = 0

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def current(cls) -> EndpointTransport:
        """Transport for the process's adb server (a worker's is its run's endpoint)."""
        return cls(current_adb_endpoint())

    @classmethod
    def local(cls) -> EndpointTransport:
        """Transport pinned to this machine's own server, whatever the preference says."""
        return cls(AdbEndpoint.local())

    @classmethod
    def shared(cls, endpoint: AdbEndpoint | None = None) -> EndpointTransport:
        """One cached transport per endpoint (``None``: the process's current one).

        Cheap to call on a hot path, and it reuses the adbutils client. The cache is
        bounded; a host endpoint gets a new entry per tunnel generation.
        """
        endpoint = endpoint if endpoint is not None else current_adb_endpoint()
        with _SHARED_LOCK:
            transport = _SHARED.get(endpoint)
            if transport is None:
                if len(_SHARED) >= _SHARED_LIMIT:
                    _SHARED.clear()
                transport = _SHARED[endpoint] = cls(endpoint)
            return transport

    def __repr__(self) -> str:
        return f"EndpointTransport({self.endpoint.identity})"

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #

    @property
    def is_local(self) -> bool:
        return self.endpoint.is_local_default

    @property
    def is_host(self) -> bool:
        return self.endpoint.is_host

    @property
    def _spawn_guarded(self) -> bool:
        """A loopback port that is not the default server: refused means not running.

        The adb *client* binary starts a server there itself, so subprocess calls get a
        reachability pre-check. A remote address is never started by the adb binary;
        adbutils is guarded separately and for every non-local endpoint (see ``client``).
        """
        return self.endpoint.is_loopback and not self.endpoint.is_local_default

    def require_local(self, operation: str) -> None:
        if not self.is_local:
            raise LocalOnlyOperation(
                f"'{operation}' acts on this computer's own adb server and cannot run "
                f"against {self.endpoint.mode} endpoint {self.endpoint.identity}."
            )

    @staticmethod
    def adb_binary() -> str | None:
        """Path of the adb executable this machine uses, or ``None`` when there is none."""
        from artemis.toolchain import toolchain

        return toolchain.resolve("adb")

    # ------------------------------------------------------------------ #
    # Reachability
    # ------------------------------------------------------------------ #

    def reachable(self, timeout: float | None = None) -> bool:
        try:
            with socket.create_connection(
                (self.endpoint.host, self.endpoint.port),
                timeout=timeout or self._probe_timeout,
            ):
                return True
        except OSError:
            return False

    def _preflight(self) -> None:
        if self._spawn_guarded and not self.reachable():
            raise EndpointUnreachable(
                f"The adb server at {self.endpoint.identity} is not reachable"
                + (" (host offline)." if self.is_host else ".")
            )

    # ------------------------------------------------------------------ #
    # adb subprocess
    # ------------------------------------------------------------------ #

    @property
    def session(self) -> AdbSession:
        return AdbSession(self.endpoint, adb_path=self._adb_path)

    def command(self, arguments: Sequence[str]) -> list[str]:
        """``adb -H host -P port <arguments>`` for this endpoint."""
        return self.session.command(arguments)

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Process environment that points every adb-aware child at this endpoint."""
        return self.session.environment(base)

    def run(self, arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self._preflight()
        return self.session.run(arguments, **kwargs)

    def popen(self, arguments: Sequence[str], **kwargs: Any) -> subprocess.Popen:
        self._preflight()
        kwargs.setdefault("env", self.environment())
        return subprocess.Popen(self.command(arguments), **kwargs)  # noqa: S603

    async def create_subprocess(
        self, arguments: Sequence[str], **kwargs: Any
    ) -> asyncio.subprocess.Process:
        self._preflight()
        kwargs.setdefault("env", self.environment())
        return await asyncio.create_subprocess_exec(*self.command(arguments), **kwargs)

    # ------------------------------------------------------------------ #
    # adbutils
    # ------------------------------------------------------------------ #

    def client(self) -> AdbClient:
        """Explicit adbutils client for this endpoint (never the module-global one)."""
        if self._client is None:
            self._client = _build_client(self.endpoint, spawn_allowed=self.is_local)
        return self._client

    def device(self, serial: str) -> AdbDevice:
        return self.client().device(serial)

    def device_list(self) -> list[AdbDevice]:
        return self.client().device_list()

    def open_stream(self, serial: str, port: int, timeout: float | None = 5.0) -> socket.socket:
        """A raw stream to ``tcp:<port>`` on the device, through the adb server.

        This is what replaces ``adb forward`` plus an HTTP call to a loopback
        port: the data never leaves the adb server connection, so it works the
        same whether that server is local or sits behind a tunnel. The caller
        owns the socket and must close it (:meth:`close_stream`).
        """
        from adbutils import Network

        self._preflight()
        stream = self.device(serial).create_connection(Network.TCP, port)
        if timeout is not None:
            stream.settimeout(timeout)
        with self._streams_lock:
            self._open_streams += 1
        return stream

    def close_stream(self, stream: socket.socket) -> None:
        try:
            stream.close()
        finally:
            with self._streams_lock:
                self._open_streams = max(0, self._open_streams - 1)

    @property
    def open_stream_count(self) -> int:
        """Streams opened through this transport and not yet closed (leak checks)."""
        with self._streams_lock:
            return self._open_streams

    # ------------------------------------------------------------------ #
    # uiautomator2
    # ------------------------------------------------------------------ #

    def u2_connect(self, serial: str) -> Any:
        """``uiautomator2.connect`` bound to this endpoint's device, not adbutils' global."""
        import uiautomator2 as u2

        return u2.connect(self.device(serial))

    # ------------------------------------------------------------------ #
    # Local-only operations
    # ------------------------------------------------------------------ #

    def start_server(self, **kwargs: Any) -> subprocess.CompletedProcess:
        self.require_local("start-server")
        return self.run(["start-server"], **kwargs)

    def kill_server(self, **kwargs: Any) -> subprocess.CompletedProcess:
        self.require_local("kill-server")
        return self.run(["kill-server"], **kwargs)


def _build_client(endpoint: AdbEndpoint, *, spawn_allowed: bool) -> AdbClient:
    from adbutils import AdbClient
    from adbutils._adb import AdbConnection
    from adbutils.errors import AdbTimeout

    if spawn_allowed:
        client = AdbClient(host=endpoint.host, port=endpoint.port)
        client.artemis_endpoint = endpoint
        return client

    class _NoSpawnConnection(AdbConnection):
        def _safe_connect(self):  # adbutils would start a local server here
            return self._create_socket()

    class _NoSpawnClient(AdbClient):
        def make_connection(self, timeout: float | None = None):
            try:
                connection = _NoSpawnConnection(self.host, self.port)
                if timeout:
                    connection.conn.settimeout(timeout)
                return connection
            except TimeoutError as error:
                raise AdbTimeout("connect to adb server timeout") from error

    client = _NoSpawnClient(host=endpoint.host, port=endpoint.port)
    client.artemis_endpoint = endpoint
    return client
