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

"""HTTP to the accessibility helper over an adb-server ``tcp:PORT`` stream.

The helper serves HTTP on a loopback port of the phone. Reaching it with
``adb forward`` plus an HTTP call to a host port only works when adb runs on
the same machine as Artemis: against a remote adb server the forward listens on
*that* machine, so the request lands nowhere (or on an unrelated local
service). A stream opened through the adb server (``host:transport`` followed by
``tcp:PORT``) travels the same connection as every other adb call, so it follows
the endpoint wherever it points.

Each request opens a stream and closes it before returning, so nothing is left
to clean up when a run ends or a tunnel drops; the transport counts open streams
so tests can prove it.
"""

from __future__ import annotations

import http.client
from io import BytesIO
import urllib.error

from adbutils.errors import AdbError

from artemis.runtime.endpoint_transport import EndpointTransport

#: Host header the helper sees; it only ever answered on loopback.
_HELPER_HOST = "127.0.0.1"


class _StreamConnection(http.client.HTTPConnection):
    """``HTTPConnection`` whose socket is an adb stream to the device port."""

    def __init__(
        self, transport: EndpointTransport, serial: str, port: int, timeout: float
    ) -> None:
        super().__init__(_HELPER_HOST, port, timeout=timeout)
        self._transport = transport
        self._serial = serial

    def connect(self) -> None:
        self.sock = self._transport.open_stream(self._serial, self.port, self.timeout)

    def close(self) -> None:
        stream = self.sock
        super().close()
        if stream is not None:
            self._transport.close_stream(stream)


def request(
    transport: EndpointTransport,
    serial: str,
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 6.0,
) -> bytes:
    """One helper request. Returns the body of a 2xx/3xx answer.

    Raises ``urllib.error.HTTPError`` for an HTTP error status and
    ``urllib.error.URLError`` when no stream or answer could be had: the same
    two failures the helper client already distinguishes (a new tunnel can fix
    the second, never the first).
    """
    connection = _StreamConnection(transport, serial, port, timeout)
    url = f"adb://{transport.endpoint.identity}/{serial}:{port}{path}"
    try:
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException, AdbError) as exc:
            raise urllib.error.URLError(exc) from exc
        if response.status >= 400:
            raise urllib.error.HTTPError(
                url, response.status, response.reason, response.headers, BytesIO(payload)
            )
        return payload
    finally:
        connection.close()
