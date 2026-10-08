import asyncio
from copy import deepcopy
import io
import logging
from unittest.mock import Mock

import pytest
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol
from uvicorn.server import ServerState

from apps.admin_console.server import REDACTED_UVICORN_LOGGING, proxy_aware_app
from artemis.utils.redaction import REDACTED, Redactor, bind_session


@pytest.fixture
def access_logging(monkeypatch):
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for attribute in ("handlers", "level", "propagate", "disabled"):
            monkeypatch.setattr(logger, attribute, getattr(logger, attribute))
    output = io.StringIO()
    log_config = deepcopy(REDACTED_UVICORN_LOGGING)
    for handler in log_config["handlers"].values():
        handler["stream"] = output
    for formatter in log_config["formatters"].values():
        formatter["use_colors"] = False
    config = uvicorn.Config(proxy_aware_app, log_config=log_config, lifespan="off", http="h11")
    config.load()
    records = []

    def capture_record(record):
        records.append(record)
        return True

    logging.getLogger("uvicorn.access").handlers[0].addFilter(capture_record)
    bind_session(None)
    yield config, output, records
    bind_session(None)


async def request_through_server(config, target):
    server_state = ServerState()
    protocol = H11Protocol(config=config, server_state=server_state, app_state={})
    transport = Mock(spec=asyncio.Transport)
    transport.is_closing.return_value = False
    transport.get_extra_info.side_effect = {
        "sockname": ("127.0.0.1", 8000),
        "peername": ("127.0.0.1", 41234),
        "sslcontext": None,
        "socket": None,
    }.get
    protocol.connection_made(transport)
    try:
        protocol.data_received(
            f"GET {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        await asyncio.wait_for(asyncio.gather(*server_state.tasks), timeout=5)
        return b"".join(call.args[0] for call in transport.write.call_args_list)
    finally:
        protocol.connection_lost(None)


@pytest.mark.asyncio
async def test_api_request_emits_redacted_well_formed_access_line(
    access_logging, monkeypatch, capsys
):
    config, output, records = access_logging
    monkeypatch.setenv("OPENAI_API_KEY", "access-configured-credential-sentinel")
    bind_session("access-test", "Login with password=access-goal-password-sentinel")
    response = await request_through_server(
        config,
        "/openapi.json?password=access-goal-password-sentinel"
        "&api_key=access-configured-credential-sentinel",
    )

    assert response.startswith(b"HTTP/1.1 200 OK")
    rendered = output.getvalue()
    assert f'127.0.0.1:41234 - "GET /openapi.json?password={REDACTED}' in rendered
    # The redacted access formatter keeps the numeric status; the status phrase is cosmetic.
    assert 'HTTP/1.1" 200' in rendered
    assert "session_id=access-test" in rendered
    assert "access-goal-password-sentinel" not in rendered
    assert "access-configured-credential-sentinel" not in rendered
    assert "Logging error" not in capsys.readouterr().err
    assert len(records) == 1
    assert isinstance(records[0].args, tuple)
    assert len(records[0].args) == 5
    assert records[0].args[-1] == 200
    assert records[0].session_id == "access-test"


@pytest.mark.parametrize("failure_point", ["message", "arguments", "extra"])
@pytest.mark.asyncio
async def test_api_request_keeps_safe_access_line_when_redaction_fails(
    access_logging, monkeypatch, capsys, failure_point
):
    config, output, records = access_logging
    original_redact = Redactor.redact

    def broken_redact(self, value):
        if (
            failure_point == "message"
            or (failure_point == "arguments" and isinstance(value, tuple))
            or (failure_point == "extra" and value == "uvicorn.access")
        ):
            raise RuntimeError("password=filter-failure-sentinel")
        return original_redact(self, value)

    monkeypatch.setattr(Redactor, "redact", broken_redact)
    response = await request_through_server(config, "/openapi.json?password=request-sentinel")

    assert response.startswith(b"HTTP/1.1 200 OK")
    rendered = output.getvalue()
    assert "REDACTED" in rendered
    assert "request-sentinel" not in rendered
    assert "filter-failure-sentinel" not in rendered
    assert "Logging error" not in capsys.readouterr().err
    assert len(records) == 1
    assert isinstance(records[0].args, tuple)
    assert len(records[0].args) == 5
    assert isinstance(records[0].args[-1], int)
