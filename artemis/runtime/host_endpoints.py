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

"""Where each host agent's adb tunnel currently listens.

A queued run names its host by id (:attr:`AdbEndpoint.host_id`), never by the
tunnel's port, because the port changes whenever the tunnel is rebuilt. The
tunnel layer registers each connection here with a new ``generation``; work
resolves its endpoint **at launch**, and anything bound to an older generation
can ask :meth:`HostEndpointRegistry.is_current` and drop itself.

Nothing registers an endpoint until the host agent lands (release B), so with
``ARTEMIS_HOST_AGENT`` off this registry is always empty.
"""

from __future__ import annotations

import threading

from artemis.runtime.adb_endpoint import AdbEndpoint


class HostOffline(RuntimeError):
    """The host a run is bound to has no live tunnel right now."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        super().__init__(f"Host '{host_id}' is offline.")


class HostEndpointRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._endpoints: dict[str, AdbEndpoint] = {}

    def register(self, endpoint: AdbEndpoint) -> None:
        """Record ``endpoint`` as its host's live tunnel; a stale generation is ignored."""
        if endpoint.host_id is None:
            raise ValueError("Only host endpoints can be registered.")
        with self._lock:
            current = self._endpoints.get(endpoint.host_id)
            if current is None or endpoint.generation >= current.generation:
                self._endpoints[endpoint.host_id] = endpoint

    def unregister(self, host_id: str, generation: int | None = None) -> None:
        """Forget a host's tunnel (only the given generation when one is named)."""
        with self._lock:
            current = self._endpoints.get(host_id)
            if current is not None and (generation is None or current.generation == generation):
                del self._endpoints[host_id]

    def resolve(self, host_id: str) -> AdbEndpoint:
        with self._lock:
            endpoint = self._endpoints.get(host_id)
        if endpoint is None:
            raise HostOffline(host_id)
        return endpoint

    def is_current(self, endpoint: AdbEndpoint) -> bool:
        """Whether ``endpoint`` is still its host's live tunnel (same generation)."""
        if endpoint.host_id is None:
            return True
        with self._lock:
            return self._endpoints.get(endpoint.host_id) == endpoint


host_endpoints = HostEndpointRegistry()
