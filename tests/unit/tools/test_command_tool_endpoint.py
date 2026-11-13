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


# --------------------------------------------------------------------------- #
# A bound client that names no usable endpoint fails closed
# --------------------------------------------------------------------------- #

UNUSABLE_CLIENTS = [
    pytest.param(("remote.example", 70000), id="invalid-port"),
    pytest.param(("http://remote.example", 5037), id="invalid-host"),
    pytest.param(("fe80::1%eth0", 5037), id="scoped-ipv6"),
]
TOOLS = [
    pytest.param(get_run_adb_command_tool, {"WaitMsBeforeAsync": 500}, id="run_adb_command"),
    pytest.param(get_run_short_adb_command_tool, {}, id="run_short_adb_command"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("factory, extra", TOOLS)
@pytest.mark.parametrize("bound", UNUSABLE_CLIENTS)
async def test_a_bound_client_without_a_usable_endpoint_never_falls_back_to_the_process_endpoint(
    factory, extra, bound, context_on_b, monkeypatch
):
    from artemis.core.tool_failure import is_tool_failure

    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "0")
    context_on_b.adb_client = AdbClient(*bound)

    with patch("asyncio.create_subprocess_exec") as spawn:
        # While the defect exists the tool reaches the spawn: answer it so the test fails, not hangs.
        spawn.return_value = MockProcess(output_bytes=b"ok\n===EXIT_CODE===0\n", exit_code=0)
        result = await factory(context_on_b).ainvoke({"CommandLine": "echo ok", **extra})

    spawn.assert_not_called()
    assert is_tool_failure(result)
    assert "endpoint" in str(result).lower()


def test_context_transport_raises_a_typed_error_for_an_unusable_client(context_on_b):
    from artemis.tools.command_tool import ContextEndpointError, context_transport

    context_on_b.adb_client = AdbClient("remote.example", 70000)

    with pytest.raises(ContextEndpointError):
        context_transport(context_on_b)
