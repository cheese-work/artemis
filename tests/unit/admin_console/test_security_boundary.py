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

"""Security regression tests for the Artemis console boundary.

Covers the invariants the no-auth security model depends on:
- secrets never appear in HTTP responses,
- media endpoints cannot read files outside the media allowlist,
- cross-origin browser traffic and DNS-rebinding Hosts are rejected,
- no CORS grants exist,
- lifecycle controls stay loopback-only.
"""

import json
import secrets as py_secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from apps.admin_console.routers import system
from apps.admin_console.server import app, proxy_aware_app
from artemis.config import TRACES_PATH, WORKSPACE_ROOT
from artemis.core.diagnostics.adb_server_connection import AdbServerConnectionManager
from artemis.core.diagnostics.engine import ReadinessEngine
from artemis.core.diagnostics.probes.credentials_probe import LLMCredentialsProbe
from artemis.core.diagnostics.probes.runtime_probe import SystemConfigProbe


def _client(**transport_kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, **transport_kwargs), base_url="http://localhost"
    )


# ---------------------------------------------------------------------------
# Secret exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_endpoint_never_returns_key_material(monkeypatch):
    from artemis.config import settings

    honeytoken = f"sk-honeytoken-{py_secrets.token_hex(16)}"
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: SecretStr(honeytoken))

    async with _client() as ac:
        res = await ac.get("/api/system/credentials")

    assert res.status_code == 200
    assert honeytoken not in res.text
    providers = {entry["name"]: entry["configured"] for entry in res.json()["providers"]}
    assert providers.get("google") is True
    assert "config_writes_locked" not in res.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/system/credentials",
            {"provider": "google", "api_key": "FAKE-CREDENTIAL-ONLY"},
        ),
        (
            "/api/system/credentials/test",
            {"provider": "google", "api_key": "FAKE-CREDENTIAL-ONLY"},
        ),
    ],
)
async def test_locked_credential_writes_return_403_without_side_effects(monkeypatch, path, payload):
    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.server import app
    from artemis.config import settings
    from artemis.utils import credentials_validator

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())

    validate_api_key = AsyncMock(return_value=(True, "valid"))
    set_api_key = MagicMock()
    monkeypatch.setattr(credentials_validator, "validate_api_key", validate_api_key)
    monkeypatch.setattr(type(settings), "set_api_key", set_api_key)

    async with _client() as ac:
        res = await ac.post(path, json=payload)

    assert res.status_code == 401
    assert res.json()["code"] == "not_signed_in"
    assert "FAKE-CREDENTIAL-ONLY" not in res.text
    validate_api_key.assert_not_awaited()
    set_api_key.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/api/system/adb/server/connect",
            {"host": "192.0.2.5", "port": 5037, "persist": True},
        ),
        ("/api/system/adb/server/local", None),
    ],
)
async def test_locked_adb_server_writes_return_403_without_side_effects(
    monkeypatch, tmp_path, path, payload
):
    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.server import app

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=unchanged\n", encoding="utf-8")
    manager = AdbServerConnectionManager(env_files=[env_file])
    manager._activate = MagicMock()
    manager.probe = AsyncMock(return_value={"success": True, "message": "offline probe"})
    readiness = MagicMock()
    readiness.run_all = AsyncMock()
    monkeypatch.setattr(system, "adb_server_connection", manager)
    monkeypatch.setattr(system, "readiness_engine", readiness)

    async with _client(client=("127.0.0.1", 1234)) as ac:
        if payload is None:
            response = await ac.post(path)
        else:
            response = await ac.post(path, json=payload)

    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"
    assert env_file.read_text(encoding="utf-8") == "EXISTING=unchanged\n"
    manager._activate.assert_not_called()
    manager.probe.assert_not_awaited()
    readiness.set_probe_target_serial.assert_not_called()
    readiness.invalidate_cache.assert_not_called()
    readiness.run_all.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_path", ["config_validation", "crashed_probe"])
async def test_readiness_failure_responses_redact_exception_text(
    monkeypatch, tmp_path, caplog, failure_path
):
    from artemis.config import llm, settings
    from mcp_server.tools.diagnose import _render_check

    fake_secret = "FAKE-S0-REVIEW-SECRET-9876"
    engine = ReadinessEngine()
    if failure_path == "config_validation":
        config_path = tmp_path / "artemis.jsonc"
        config_path.write_text(
            json.dumps(
                {
                    "default": {
                        "provider": "openai",
                        "model": "offline-review-model",
                        "api_base": {"key": fake_secret},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(llm, "get_config_path", lambda *_args: config_path)
        probe = SystemConfigProbe()
    else:
        probe = LLMCredentialsProbe()

        def fail(_settings, _provider):
            raise ValueError(f"Rejected credential {fake_secret}")

        monkeypatch.setattr(type(settings), "get_api_key", fail)

    engine._probes = {probe.probe_id: probe}
    monkeypatch.setattr(system, "readiness_engine", engine)
    report = await engine.run_all(force_refresh=True)

    async with _client(client=("127.0.0.1", 1234)) as ac:
        response = await ac.get("/api/system/readiness", params={"force": "true"})

    assert response.status_code == 200
    assert fake_secret not in response.text
    assert "Rejected credential" not in response.text
    assert fake_secret not in json.dumps(_render_check(report.probes[0]))
    assert fake_secret not in caplog.text


@pytest.mark.asyncio
async def test_server_status_omits_lifecycle_token_and_metadata():
    async with _client() as ac:
        res = await ac.get("/api/system/server-status")

    assert res.status_code == 200
    data = res.json()
    assert "metadata" not in data
    assert "lifecycle_token" not in res.text
    assert "cmdline" not in res.text
    assert "current_pid" in data


# ---------------------------------------------------------------------------
# File access boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_videos_endpoint_refuses_dotenv_and_non_video_files():
    async with _client() as ac:
        for target in (".env", "pyproject.toml", "artemis/__init__.py"):
            res = await ac.get(f"/videos/{target}")
            assert res.status_code in (403, 404), target
            assert "API_KEY" not in res.text


@pytest.mark.asyncio
async def test_videos_endpoint_refuses_encoded_traversal():
    async with _client() as ac:
        for target in (
            "%2e%2e/%2e%2e/etc/passwd",
            "..%5c..%5cwindows%5cwin.ini",
            "%2e%2e%2f.env",
        ):
            res = await ac.get(f"/videos/{target}")
            assert res.status_code in (403, 404), target


@pytest.mark.asyncio
async def test_videos_endpoint_still_serves_real_recordings():
    TRACES_PATH.mkdir(parents=True, exist_ok=True)
    probe = TRACES_PATH / f"security-probe-{py_secrets.token_hex(4)}.mp4"
    probe.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    try:
        async with _client() as ac:
            res = await ac.get(f"/videos/{probe.name}")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("video/mp4")
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_local_file_endpoint_refuses_non_media_workspace_files():
    async with _client() as ac:
        for target in (
            str(WORKSPACE_ROOT / ".env"),
            str(WORKSPACE_ROOT / "pyproject.toml"),
            "file://" + str(WORKSPACE_ROOT / ".env"),
        ):
            res = await ac.get("/local_file", params={"path": target})
            assert res.status_code in (403, 404), target
            assert "API_KEY" not in res.text


@pytest.mark.asyncio
async def test_spa_route_does_not_leak_files_outside_static_roots():
    async with _client() as ac:
        res = await ac.get("/%2e%2e/%2e%2e/pyproject.toml")

    # The catch-all SPA route must fall back to HTML, never the file content.
    assert "requires-python" not in res.text
    assert res.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Browser boundary: Host, Origin, CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_rebinding_host_is_rejected():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://attacker.example"
    ) as ac:
        res = await ac.get("/api/system/emulator/status")

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_cross_origin_browser_request_is_rejected_without_cors_grant():
    async with _client() as ac:
        res = await ac.post(
            "/api/system/emulator/dismiss",
            headers={"Origin": "https://attacker.example"},
        )

    assert res.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}


@pytest.mark.asyncio
async def test_same_origin_browser_request_passes():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status", headers={"Origin": "http://localhost"})

    assert res.status_code == 200


@pytest.mark.asyncio
async def test_null_origin_is_rejected():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status", headers={"Origin": "null"})

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_security_headers_present_and_api_responses_uncacheable():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status")

    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.headers.get("referrer-policy") == "no-referrer"
    assert res.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Lifecycle controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_submission_denial_has_no_side_effects(monkeypatch):
    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.routers import tasks as task_router

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())
    enqueue = AsyncMock()
    readiness_probe = AsyncMock(return_value=None)
    monkeypatch.setattr(task_router.task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(
        task_router.readiness_engine, "run_device_submission_probe", readiness_probe
    )

    async with _client(client=("203.0.113.9", 51000)) as ac:
        response = await ac.post("/api/run", json={"goal": "must not enqueue"})

    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"
    enqueue.assert_not_awaited()
    readiness_probe.assert_not_awaited()


def test_device_bridge_denial_has_no_side_effects(monkeypatch):
    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.routers import device_bridge

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())
    service = MagicMock()
    service.create_session = AsyncMock()
    monkeypatch.setattr(device_bridge, "bridge_session_service", service)
    client = TestClient(proxy_aware_app, client=("127.0.0.1", 51000))

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect("/api/device-bridge/session", headers={"Host": "127.0.0.1"}):
            pass

    assert raised.value.code == 1008
    service.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_requires_admin_before_starting_worker():
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value = MagicMock()
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
            base_url="http://localhost",
        ) as ac:
            res = await ac.post("/api/system/restart")

        assert res.status_code == 401
        mock_thread.assert_not_called()


@pytest.mark.asyncio
async def test_config_write_denial_has_no_side_effects(monkeypatch):
    from apps.admin_console.core.access_control import AccessConfig, CloudflareAccessVerifier
    from apps.admin_console.routers import system as system_router
    from apps.admin_console.server import app

    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="test-audience",
            issuer="https://team.cloudflareaccess.com",
            admin_emails=frozenset({"admin@example.com"}),
        ),
    )
    monkeypatch.setattr(app.state, "access_verifier", CloudflareAccessVerifier())
    config_store_factory = MagicMock()
    monkeypatch.setattr(system_router, "get_config_store", config_store_factory)

    async with _client() as ac:
        response = await ac.put(
            "/api/system/config",
            json={
                "version": "version-1",
                "default": {"provider": "openai", "model": "gpt-test"},
                "credentials": {"openai": "FAKE-SECRET-MUST-NOT-ECHO"},
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "not_signed_in"
    assert "FAKE-SECRET-MUST-NOT-ECHO" not in response.text
    config_store_factory.assert_not_called()
