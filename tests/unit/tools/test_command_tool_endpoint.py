"""Agent shell tools address the run context's adb endpoint, not the process preference (CHE-1094)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from adbutils import AdbClient
import pytest

from artemis.config import settings
from artemis.context import ArtemisContext
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.tools.command_tool import get_run_adb_command_tool, get_run_short_adb_command_tool
from tests.unit.tools.test_command_tool import MockProcess

SERIAL = "emulator-5554"


@pytest.fixture
def context_on_b(monkeypatch):
    """Process preference is server A (40001); the run's context is bound to server B (40002)."""
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 40001)
    monkeypatch.setattr(EndpointTransport, "reachable", lambda self, timeout=None: True)
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = None
    ctx.device = MagicMock()
    ctx.device.device_id = SERIAL
    ctx.adb_client = AdbClient("127.0.0.1", 40002)
    return ctx


def _server_of(argv: tuple) -> tuple[str, str]:
    assert "-H" in argv and "-P" in argv
    return argv[argv.index("-H") + 1], argv[argv.index("-P") + 1]


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_adb_command_uses_the_contexts_endpoint(mock_exec, context_on_b):
    mock_exec.return_value = MockProcess(output_bytes=b"ok\n===EXIT_CODE===0\n", exit_code=0)

    await get_run_adb_command_tool(context_on_b).ainvoke(
        {"CommandLine": "echo ok", "Cwd": "/data/local/tmp", "WaitMsBeforeAsync": 500}
    )

    args, _kwargs = mock_exec.call_args
    assert _server_of(args) == ("127.0.0.1", "40002")
    assert args[args.index("-s") + 1] == SERIAL


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_short_adb_command_uses_the_contexts_endpoint(mock_exec, context_on_b):
    mock_exec.return_value = MockProcess(output_bytes=b"ok", exit_code=0)

    await get_run_short_adb_command_tool(context_on_b).ainvoke({"CommandLine": "echo ok"})

    args, _kwargs = mock_exec.call_args
    assert _server_of(args) == ("127.0.0.1", "40002")


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_a_context_without_an_adb_client_follows_the_process_endpoint(
    mock_exec, context_on_b
):
    context_on_b.adb_client = None
    mock_exec.return_value = MockProcess(output_bytes=b"ok", exit_code=0)

    await get_run_short_adb_command_tool(context_on_b).ainvoke({"CommandLine": "echo ok"})

    args, _kwargs = mock_exec.call_args
    assert _server_of(args) == ("127.0.0.1", "40001")


def test_a_transport_built_client_carries_its_endpoint_including_host_identity(monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "1")
    from artemis.runtime.adb_endpoint import AdbEndpoint
    from artemis.tools.command_tool import context_transport

    endpoint = AdbEndpoint.create("127.0.0.1", 40003, host_id="lab-1", generation=2)
    ctx = MagicMock(spec=ArtemisContext)
    ctx.adb_client = EndpointTransport.shared(endpoint).client()

    assert context_transport(ctx).endpoint == endpoint
