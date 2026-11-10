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

"""Immutable ADB endpoint and task target primitives.

The selected endpoint is a user preference. An :class:`AdbTarget` is an
execution snapshot. Keeping those concepts separate prevents a queued or
running task from silently moving to another ADB server when the preference
changes in the Admin Console.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess
from typing import Any, Mapping, MutableMapping, Sequence

from artemis.config import settings
from artemis.config.constants import DEFAULT_ADB_HOST, DEFAULT_ADB_PORT
from artemis.config.host_agent import host_agent_enabled
from artemis.toolchain import toolchain


ADB_ENDPOINT_ID_ENV = "ARTEMIS_ADB_ENDPOINT_ID"
ADB_HOST_ID_ENV = "ARTEMIS_ADB_HOST_ID"
ADB_GENERATION_ENV = "ARTEMIS_ADB_GENERATION"
#: Variables adbutils (and therefore uiautomator2's global client) read to find the server.
ANDROID_ADB_SERVER_HOST_ENV = "ANDROID_ADB_SERVER_HOST"
ANDROID_ADB_SERVER_PORT_ENV = "ANDROID_ADB_SERVER_PORT"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SAFE_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
_HOST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class InvalidAdbEndpoint(ValueError):
    """Raised when an ADB server endpoint is malformed."""


@dataclass(frozen=True, slots=True)
class AdbEndpoint:
    """Network address of one ADB server.

    A *host endpoint* (``host_id`` set) is the server-side loopback listener of a
    host agent's tunnel. Its address changes whenever the tunnel is rebuilt, so
    the durable identity (device lock scope, queue snapshots) is the host id and
    never the port; ``generation`` counts tunnel rebuilds so a callback bound to
    an older connection can be recognised and dropped. A host endpoint is never
    written to ``.env`` and never targets local-only adb operations.
    """

    host: str
    port: int
    host_id: str | None = None
    generation: int = 0

    @classmethod
    def create(
        cls,
        host: str,
        port: int,
        *,
        host_id: str | None = None,
        generation: int = 0,
    ) -> AdbEndpoint:
        clean_host = str(host).strip()
        if clean_host.startswith("[") and clean_host.endswith("]"):
            clean_host = clean_host[1:-1]
        if not clean_host:
            raise InvalidAdbEndpoint("ADB server host cannot be empty.")
        if len(clean_host) > 253 or not _SAFE_HOST_PATTERN.fullmatch(clean_host):
            raise InvalidAdbEndpoint(
                "Enter a valid IP address or host name without a URL scheme or path."
            )
        if not 1 <= int(port) <= 65535:
            raise InvalidAdbEndpoint("ADB server port must be between 1 and 65535.")
        clean_host_id: str | None = None
        if host_id is not None:
            if not host_agent_enabled():
                raise InvalidAdbEndpoint("Host agent endpoints are disabled (ARTEMIS_HOST_AGENT).")
            clean_host_id = str(host_id).strip()
            if not _HOST_ID_PATTERN.fullmatch(clean_host_id):
                raise InvalidAdbEndpoint("Host id must be 1-64 letters, digits, '-' or '_'.")
            if clean_host.lower() not in _LOCAL_HOSTS:
                raise InvalidAdbEndpoint("A host agent tunnel listens on loopback only.")
        return cls(
            host=clean_host,
            port=int(port),
            host_id=clean_host_id,
            generation=max(0, int(generation)),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AdbEndpoint:
        raw_host_id = value.get("host_id")
        return cls.create(
            str(value.get("host", "")),
            int(value.get("port", 0)),
            host_id=str(raw_host_id) if raw_host_id else None,
            generation=int(value.get("generation") or 0),
        )

    @classmethod
    def local(cls) -> AdbEndpoint:
        return cls(DEFAULT_ADB_HOST, DEFAULT_ADB_PORT)

    @property
    def socket(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"tcp:{host}:{self.port}"

    @property
    def is_host(self) -> bool:
        return self.host_id is not None

    @property
    def identity(self) -> str:
        """Stable identity used for task snapshots and device-lock scoping.

        A host endpoint is identified by its host, not its (ephemeral) port.
        """
        if self.host_id is not None:
            return f"host:{self.host_id}".lower()
        return self.socket.lower()

    @property
    def is_local_default(self) -> bool:
        return (
            self.host_id is None
            and self.host.lower() in _LOCAL_HOSTS
            and self.port == DEFAULT_ADB_PORT
        )

    @property
    def is_loopback(self) -> bool:
        return self.host.lower() in _LOCAL_HOSTS

    @property
    def lock_scope(self) -> str:
        """Scope of device locks and queue records taken on this endpoint."""
        return self.identity

    @property
    def persistable(self) -> bool:
        """Whether this endpoint may be saved as the user's adb server preference."""
        return self.host_id is None

    @property
    def mode(self) -> str:
        if self.is_host:
            return "host"
        return "local" if self.is_local_default else "remote"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "host": self.host,
            "port": self.port,
            "socket": self.socket,
            "identity": self.identity,
            "mode": self.mode,
            "is_local_default": self.is_local_default,
        }
        if self.is_host:
            payload["host_id"] = self.host_id
            payload["generation"] = self.generation
        return payload

    def apply_to_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> MutableMapping[str, str]:
        target = environment if environment is not None else os.environ
        target["ADB_HOST"] = self.host
        target["ADB_PORT"] = str(self.port)
        target["ADB_SERVER_SOCKET"] = self.socket
        # adbutils' global client (what ``uiautomator2.connect(serial)`` uses) reads these.
        target[ANDROID_ADB_SERVER_HOST_ENV] = self.host
        target[ANDROID_ADB_SERVER_PORT_ENV] = str(self.port)
        target[ADB_ENDPOINT_ID_ENV] = self.identity
        if self.host_id is not None:
            target[ADB_HOST_ID_ENV] = self.host_id
            target[ADB_GENERATION_ENV] = str(self.generation)
        else:
            target.pop(ADB_HOST_ID_ENV, None)
            target.pop(ADB_GENERATION_ENV, None)
        return target


@dataclass(frozen=True, slots=True)
class AdbTarget:
    """A device serial bound to the ADB endpoint that discovered it."""

    endpoint: AdbEndpoint
    serial: str | None = None
    # A host-agent device is locked by host id + opaque device id, not by the
    # (loopback) endpoint that happens to carry its traffic.
    host_id: str | None = None

    @property
    def lock_scope(self) -> str:
        """Transport lock scope; physical-device admission is a separate reservation."""
        return f"host:{self.host_id}" if self.host_id else self.endpoint.lock_scope

    @property
    def lock_key(self) -> str:
        return f"{self.lock_scope}/{self.serial or 'pending'}"

    def to_dict(self) -> dict[str, Any]:
        data = {"endpoint": self.endpoint.to_dict(), "serial": self.serial}
        if self.host_id:
            data["host_id"] = self.host_id
        return data


class AdbSession:
    """Execute ADB commands against one explicit, immutable endpoint."""

    def __init__(self, endpoint: AdbEndpoint, adb_path: str | None = None) -> None:
        self.endpoint = endpoint
        self.adb_path = adb_path or toolchain.resolve("adb")

    def command(self, arguments: Sequence[str]) -> list[str]:
        if not self.adb_path:
            raise FileNotFoundError("Android Platform Tools (adb) could not be found.")
        return [
            self.adb_path,
            "-H",
            self.endpoint.host,
            "-P",
            str(self.endpoint.port),
            *arguments,
        ]

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(base if base is not None else os.environ)
        self.endpoint.apply_to_environment(environment)
        return environment

    def run(self, arguments: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        kwargs.setdefault("env", self.environment())
        return subprocess.run(self.command(arguments), **kwargs)


def current_adb_endpoint() -> AdbEndpoint:
    """Return the process preference as an immutable endpoint snapshot."""
    host = settings.ADB_HOST or os.environ.get("ADB_HOST") or DEFAULT_ADB_HOST
    port = settings.ADB_PORT or os.environ.get("ADB_PORT") or DEFAULT_ADB_PORT
    host_id = os.environ.get(ADB_HOST_ID_ENV)
    if host_id and host_agent_enabled():
        return AdbEndpoint.create(
            str(host),
            int(port),
            host_id=host_id,
            generation=int(os.environ.get(ADB_GENERATION_ENV) or 0),
        )
    return AdbEndpoint.create(str(host), int(port))
