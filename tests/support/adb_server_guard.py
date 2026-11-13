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

"""Refuse any default-suite call that would reach this machine's own adb server.

``make test`` is documented as needing no device. An adb client call to the
default server (loopback, port 5037) starts that server when it is down and
queries the attached phones when it is up, which skirts the Android
device-claim rule. Fake servers listen on ephemeral ports and remote hosts use
their own address, so neither is touched; ``adb version`` and friends never
contact a server.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import contextlib
import os
import socket
import subprocess

DEFAULT_ADB_PORT = 5037
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})
CLIENT_ONLY_COMMANDS = frozenset({"version", "--version", "help", "--help"})
GUARD_MESSAGE = "reached the real adb server"


def _refusal(what: str) -> RuntimeError:
    return RuntimeError(
        f"Test {GUARD_MESSAGE} ({what}). Mock the adb access, point it at a "
        "fake_adb_server_factory server, or mark the test android."
    )


def _option(arguments: Sequence[str], flag: str) -> str | None:
    for index, value in enumerate(arguments[:-1]):
        if value == flag:
            return arguments[index + 1]
    return None


def reaches_default_server(command: Sequence[str]) -> bool:
    """True for an ``adb [-H host] [-P port] <command>`` call aimed at the local default server."""
    if not command or os.path.basename(str(command[0])) not in {"adb", "adb.exe"}:
        return False
    arguments = [str(argument) for argument in command[1:]]
    if not arguments or arguments[0] in CLIENT_ONLY_COMMANDS:
        return False
    host = _option(arguments, "-H") or os.environ.get("ANDROID_ADB_SERVER_ADDRESS", "")
    port = _option(arguments, "-P") or os.environ.get("ANDROID_ADB_SERVER_PORT", "")
    return host in LOOPBACK_HOSTS and int(port or DEFAULT_ADB_PORT) == DEFAULT_ADB_PORT


@contextlib.contextmanager
def refuse_default_adb_server() -> Iterator[None]:
    """Make subprocess and socket access to the local default adb server raise."""
    # Tests may swap ``subprocess.Popen`` for a mock while the guard is active: restore the class patched.
    popen, popen_init = subprocess.Popen, subprocess.Popen.__init__
    sock, socket_connect = socket.socket, socket.socket.connect

    def guarded_popen_init(self, args, *positional, **keywords):
        if isinstance(args, (list, tuple)) and reaches_default_server(args):
            raise _refusal(" ".join(map(str, args)))
        popen_init(self, args, *positional, **keywords)

    def guarded_connect(self, address):
        if (
            isinstance(address, tuple)
            and address[0] in LOOPBACK_HOSTS
            and address[1] == DEFAULT_ADB_PORT
        ):
            raise _refusal(f"connect {address[0]}:{address[1]}")
        return socket_connect(self, address)

    popen.__init__ = guarded_popen_init
    sock.connect = guarded_connect
    try:
        yield
    finally:
        popen.__init__ = popen_init
        sock.connect = socket_connect
