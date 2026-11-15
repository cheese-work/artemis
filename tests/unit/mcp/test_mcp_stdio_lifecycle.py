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

"""Unit and integration tests verifying MCP stdio handshake reliability,

subprocess stdin isolation against pipe hijacking, and stdout purity.
"""

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from artemis.runtime.awake_lease import ScreenAwakeLease
from artemis.runtime.awake_service import _run_awake_adb_command
from third_party.mobile_use.clients import ui_automator_client
from third_party.mobile_use.utils.logger import get_logger
from mcp_server.utils import device_utils, env_utils
from tests.support.startup_budget import startup_budget


def _readline_with_timeout(pipe, timeout: float) -> str | None:
    """Read one line from a subprocess pipe with a timeout, portably.

    select.select only supports sockets on Windows, so poll via a reader thread
    instead of selecting on the pipe handle.
    """
    result: queue.Queue = queue.Queue()

    def _reader():
        try:
            result.put(pipe.readline())
        except Exception:
            result.put(b"")

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        return None
    if not line:
        return None
    return line.decode("utf-8").strip()


def test_mcp_stdio_handshake_immediate_input():
    """Verify that MCP server over stdio responds to an immediate initialize request

    without hanging, even when stdin data is pushed concurrently with process startup.
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    python_exe = sys.executable
    child_env = {
        "PYTHONUNBUFFERED": "1",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": project_root,
        # This protocol test must never discover or configure host devices.
        "ARTEMIS_KEEP_DEVICE_AWAKE": "false",
        # Windows: the interpreter's socket stack (Winsock/_overlapped),
        # Path.home(), and tempfile handling need these system variables.
        **{
            key: os.environ[key]
            for key in (
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "APPDATA",
                "LOCALAPPDATA",
            )
            if key in os.environ
        },
    }
    budget = startup_budget(6.0, env=child_env)

    # A file cannot fill a pipe and block the child on startup diagnostics.
    with (
        tempfile.TemporaryFile() as stderr,
        subprocess.Popen(
            [python_exe, "-m", "mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=child_env,
            cwd=project_root,
        ) as p,
    ):
        try:
            # Immediately write the JSON-RPC initialize request into stdin
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0.0"},
                },
            }
            p.stdin.write(json.dumps(init_req).encode("utf-8") + b"\n")
            p.stdin.flush()

            # Read initialize response with a strict timeout
            init_resp_line = _readline_with_timeout(p.stdout, budget)

            if init_resp_line is None:
                stderr.seek(0)
                pytest.fail(
                    f"MCP server did not respond to initialize within {budget:.1f}s; "
                    f"exit={p.poll()}; stderr={stderr.read(8192).decode('utf-8', errors='replace')}"
                )
            init_data = json.loads(init_resp_line)
            assert init_data.get("id") == 1
            assert "result" in init_data

            # Follow up with initialized notification and tools/list request
            notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            p.stdin.write(json.dumps(notif).encode("utf-8") + b"\n")
            p.stdin.write(json.dumps(tools_req).encode("utf-8") + b"\n")
            p.stdin.flush()

            tools_resp_line = _readline_with_timeout(p.stdout, startup_budget(4.0, env=child_env))

            assert tools_resp_line is not None, (
                "MCP server failed to respond to tools/list request!"
            )
            tools_data = json.loads(tools_resp_line)
            assert tools_data.get("id") == 2
            tools = tools_data.get("result", {}).get("tools", [])
            tool_names = {t["name"] for t in tools}
            assert {
                "mobile_run_task",
                "mobile_manage_task",
                "mobile_get_device_state",
                "mobile_inspect_trace",
                "mobile_diagnose",
            }.issubset(tool_names)

        finally:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)


def test_mcp_stdio_handshake_with_verbose_startup(monkeypatch):
    """Startup diagnostics must not block the child before it can read stdin."""
    real_popen = subprocess.Popen

    class VerbosePopen(real_popen):
        def __init__(self, args, **kwargs):
            if args[1:] == ["-m", "mcp_server"]:
                args = [
                    args[0],
                    "-c",
                    "import runpy, sys; "
                    "sys.stderr.write('startup diagnostic\\n' * 8192); "
                    "sys.stderr.flush(); "
                    "runpy.run_module('mcp_server', run_name='__main__')",
                ]
                kwargs["env"]["ARTEMIS_KEEP_DEVICE_AWAKE"] = "false"
            super().__init__(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", VerbosePopen)
    test_mcp_stdio_handshake_immediate_input()


def test_awake_service_adb_command_isolates_stdin():
    """Verify _run_awake_adb_command always sets stdin=subprocess.DEVNULL."""
    with patch("artemis.runtime.awake_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _run_awake_adb_command("test-dev-1", ["shell", "date"], "test command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_awake_lease_run_isolates_stdin():
    """Verify ScreenAwakeLease._run always sets stdin=subprocess.DEVNULL."""
    lease = ScreenAwakeLease("test-dev-1")
    with patch("artemis.runtime.awake_lease.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        lease._run(["shell", "date"], "test lease command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_detached_process_kwargs_isolates_stdin():
    """Verify get_detached_process_kwargs always sets stdin=subprocess.DEVNULL."""
    kwargs = env_utils.get_detached_process_kwargs()
    assert kwargs.get("stdin") == subprocess.DEVNULL, (
        "Expected stdin=subprocess.DEVNULL for detached background tasks!"
    )


def test_device_utils_isolates_stdin():
    """Verify device_utils subprocess calls always set stdin=subprocess.DEVNULL."""
    with patch("mcp_server.utils.device_utils.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="List of devices attached\n", stderr=""
        )
        device_utils.get_connected_devices()
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_ui_automator_client_isolates_stdin():
    """Verify ui_automator_client helper commands isolate stdin."""
    with patch("third_party.mobile_use.clients.ui_automator_client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="package:com.test\n", stderr="")
        ui_automator_client._is_package_installed("dev-1", "com.test")
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL

    with patch("third_party.mobile_use.clients.ui_automator_client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ui_automator_client._uninstall_package("dev-1", "com.test")
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_logger_header_does_not_pollute_stdout():
    """Verify logger.header directs output to stderr and never pollutes sys.stdout."""
    logger = get_logger("test_purity_logger")
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    try:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr
        logger.header("IMPORTANT PROTOCOL BANNER")
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr

    assert captured_stdout.getvalue() == "", (
        f"Detected stdout pollution from logger.header: {captured_stdout.getvalue()!r}. "
        "In MCP stdio mode, stdout MUST be reserved exclusively for JSON-RPC messages!"
    )
    assert "IMPORTANT PROTOCOL BANNER" in captured_stderr.getvalue()
