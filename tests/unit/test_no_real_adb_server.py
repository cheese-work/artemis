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

"""The default suite must never reach this machine's own adb server."""

import socket
import subprocess

import pytest

from tests.support.adb_server_guard import GUARD_MESSAGE


@pytest.fixture
def logging_adb(tmp_path):
    """A stand-in adb that records each call; never the machine's real binary."""
    log = tmp_path / "calls.log"
    adb = tmp_path / "adb"
    adb.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    adb.chmod(0o755)
    return adb, log


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(
    "arguments",
    [
        ["-H", "127.0.0.1", "-P", "5037", "devices", "-l"],
        ["-P", "5037", "start-server"],
        ["devices"],
    ],
)
def test_subprocess_to_the_default_adb_server_is_refused(logging_adb, arguments):
    adb, log = logging_adb

    with pytest.raises(RuntimeError, match=GUARD_MESSAGE):
        subprocess.run([str(adb), *arguments], check=False)

    assert _calls(log) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["-H", "127.0.0.1", "-P", "40123", "devices"],
        ["-H", "10.0.0.7", "-P", "5037", "devices"],
        ["version"],
    ],
)
def test_other_servers_and_client_only_commands_still_run(logging_adb, arguments):
    adb, log = logging_adb

    subprocess.run([str(adb), *arguments], check=True)

    assert _calls(log) == [" ".join(arguments)]


def test_socket_to_the_default_adb_server_is_refused():
    with pytest.raises(RuntimeError, match=GUARD_MESSAGE):
        socket.create_connection(("127.0.0.1", 5037), timeout=0.1)
