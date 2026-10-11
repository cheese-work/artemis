import asyncio
import json
import logging
import stat

import httpx
import pytest

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.services import config_store as config_module
from apps.admin_console.services.config_store import ConfigStore, ConfigStoreError
from apps.admin_console.services.model_discovery import ModelDiscovery
from apps.admin_console.services.provider_settings import ProviderSettings


@pytest.fixture
def providers(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    config = tmp_path / "artemis.jsonc"
    config.write_text(
        json.dumps(
            {
                "default": {
                    "provider": "openai",
                    "model": "fixture-model",
                    "fallback": {"provider": "openai", "model": "fixture-model"},
                },
                "nodes": {},
            }
        )
    )
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=private-fixture-key\nOPENAI_BASE_URL=https://fixture.test/v1\n")
    return ProviderSettings(ConfigStore(config, env))


async def save(providers, provider="openai", **changes):
    snapshot = await providers.read()
    return await providers.save(provider, snapshot["version"], actor="admin@example.com", **changes)


def fake_protocol(provider, *, error=None):
    calls = []

    def reply(request):
        calls.append(request)
        key = (
            request.headers.get("x-goog-api-key")
            or request.headers.get("x-api-key")
            or request.headers.get("authorization", "").removeprefix("Bearer ")
        )
        assert key == "private-fixture-key"
        assert key not in str(request.url)
        if error:
            return httpx.Response(401, json={"error": {"message": f"bad key {key}"}})
        if request.method == "GET":
            expected = {"google": "/v1beta/models", "anthropic": "/v1/models"}.get(
                provider, "/v1/models"
            )
            assert request.url.path == expected
            payload = (
                {
                    "models": [
                        {
                            "name": "models/fixture-model",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
                if provider == "google"
                else {"data": [{"id": "fixture-model"}]}
            )
            return httpx.Response(200, json=payload)
        body = json.loads(request.content)
        if provider == "google":
            assert request.url.path == "/v1beta/models/fixture-model:generateContent"
            assert body["contents"][0]["parts"][0]["text"]
            payload = {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
        elif provider == "anthropic":
            assert request.url.path == "/v1/messages"
            assert body["max_tokens"] == 1
            payload = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
        else:
            assert request.url.path == "/v1/chat/completions"
            assert body["messages"][0]["content"]
            payload = {"choices": [{"message": {"content": "OK"}}]}
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(reply), calls


@pytest.mark.asyncio
async def test_migration_save_boundary_masking_and_permissions(providers):
    initial = await providers.read()
    assert len(initial["slots"]) == 5
    slot = initial["slots"][0]
    assert slot["provider"] == "openai"
    assert slot["key_preview"] == "****-key"
    assert not slot["saved"]
    assert "private-fixture-key" not in json.dumps(initial)
    providers.store.env_path.write_text("OPENAI_API_KEY=new-environment-key\n")
    assert (await providers.read())["slots"][0]["key_preview"] == "****-key"
    await save(providers, key="saved-secret", base_url="https://saved.test/v1", enabled=True)
    from artemis.utils.redaction import Redactor, redact

    record = logging.makeLogRecord({"msg": "provider said saved-secret", "args": ()})
    assert Redactor().filter(record)
    assert "saved-secret" not in record.getMessage()
    assert "saved-secret" not in json.dumps(redact({"trace": "saved-secret"}))
    providers.store.env_path.write_text("OPENAI_API_KEY=other-env-value\n")
    restarted = ProviderSettings(providers.store)
    assert (await restarted.read())["slots"][0]["saved"]
    snapshot = await providers.store.snapshot_for_spawn(
        {"provider": "openai", "model": "fixture-model"}
    )
    try:
        assert snapshot.environment["OPENAI_API_KEY"] == "saved-secret"
        assert (
            json.loads(snapshot.config_path.read_text())["nodes"]["operator"]["api_base"]
            == "https://saved.test/v1"
        )
    finally:
        providers.store.cleanup_snapshot(snapshot)
    assert stat.S_IMODE(providers.path.stat().st_mode) == 0o600
    assert "saved-secret" not in providers.store.audit_path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "google", "anthropic", "xai", "openrouter"])
async def test_native_test_sync_and_picker_for_every_slot(providers, provider):
    base = (
        "https://fixture.test" if provider in {"google", "anthropic"} else "https://fixture.test/v1"
    )
    await save(providers, provider, key="private-fixture-key", base_url=base, enabled=True)
    providers.transport, calls = fake_protocol(provider)
    result = await providers.test(provider)
    assert result["slots"][[slot["provider"] for slot in result["slots"]].index(provider)][
        "last_test"
    ]["ok"]
    assert len(calls) == 2
    result = await providers.sync(provider)
    slot = next(slot for slot in result["slots"] if slot["provider"] == provider)
    assert slot["model_count"] == 1 and slot["last_sync"]
    if provider != "openai":
        await save(providers, "openai", enabled=False)
    catalog = await ModelDiscovery(providers.store, transport=providers.transport).catalog()
    assert any(
        row["provider"] == provider and row["model"] == "fixture-model" for row in catalog["models"]
    )


@pytest.mark.asyncio
async def test_saved_unsynced_disabled_and_rotated_slots_reject_runs(providers):
    await save(providers, enabled=True)
    discovery = ModelDiscovery(providers.store)
    for selection in (None, {"provider": "openai", "model": "fixture-model"}):
        with pytest.raises(AdminAPIError):
            await discovery.resolve(selection)
    providers.transport, _calls = fake_protocol("openai")
    await providers.sync("openai")
    assert await discovery.resolve(None) == {"provider": "openai", "model": "fixture-model"}
    await save(providers, enabled=False)
    with pytest.raises(AdminAPIError):
        await discovery.resolve(None)
    snapshot = await providers.store.snapshot_for_spawn()
    try:
        assert snapshot.environment["OPENAI_API_KEY"] == ""
    finally:
        providers.store.cleanup_snapshot(snapshot)
    await save(providers, enabled=True, key="rotated-secret")
    assert (await providers.read())["slots"][0]["model_count"] == 0


@pytest.mark.asyncio
async def test_errors_are_secret_safe_and_last_good_sync_survives(providers, caplog):
    await save(providers, enabled=True)
    providers.transport, _calls = fake_protocol("openai")
    await providers.sync("openai")
    providers.transport, _calls = fake_protocol("openai", error=True)
    await providers.test("openai")
    result = await providers.sync("openai")
    assert result["slots"][0]["model_count"] == 1
    assert "HTTP 401" in result["slots"][0]["sync_error"]
    assert "private-fixture-key" not in json.dumps(result)
    assert "private-fixture-key" not in caplog.text
    assert "bad key private-fixture-key" not in providers.path.read_text()


@pytest.mark.asyncio
async def test_wrong_protocol_and_stale_save(providers):
    await save(providers, enabled=True)
    stale = (await providers.read())["version"]
    providers.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"models": []})
    )
    result = await providers.test("openai")
    assert not result["slots"][0]["last_test"]["ok"]
    assert "protocol" in result["slots"][0]["last_test"]["error"].lower()
    with pytest.raises(ConfigStoreError):
        await providers.save("openai", stale, enabled=False, actor="admin@example.com")


@pytest.mark.asyncio
async def test_provider_api_is_admin_only_and_validation_never_echoes_keys(
    providers, monkeypatch, admin, qa
):
    from apps.admin_console.routers import providers as routes

    monkeypatch.setattr(routes, "get_provider_settings", lambda: providers)
    async with qa:
        for method, path in [
            ("GET", ""),
            ("PUT", "/openai"),
            ("POST", "/openai/test"),
            ("POST", "/openai/sync"),
        ]:
            response = await qa.request(
                method, "/api/system/providers" + path, json={"key": "private-fixture-key"}
            )
            assert response.status_code == 403
            assert "private-fixture-key" not in response.text
    async with admin:
        response = await admin.put(
            "/api/system/providers/openai",
            json={"version": "bad", "enabled": "invalid", "key": "private-fixture-key"},
        )
        assert response.status_code == 400
        assert "private-fixture-key" not in response.text
        assert (await admin.get("/api/system/providers")).status_code == 200


@pytest.mark.asyncio
async def test_rotation_discards_in_flight_sync_and_saved_slots_do_not_scan(providers):
    await save(providers, enabled=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def reply(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"data": [{"id": "fixture-model"}]})

    providers.transport = httpx.MockTransport(reply)
    operation = asyncio.create_task(providers.sync("openai"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await save(providers, key="rotated-fixture-key")
    finally:
        release.set()
    with pytest.raises(ConfigStoreError) as caught:
        await operation
    assert caught.value.code == "config_conflict"
    assert (await providers.read())["slots"][0]["model_count"] == 0
    calls = []
    discovery = ModelDiscovery(
        providers.store, transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    assert (await discovery.catalog())["models"] == []
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "google", "anthropic", "xai", "openrouter"])
async def test_native_list_with_wrong_inference_protocol_fails(providers, provider):
    base = (
        "https://fixture.test" if provider in {"google", "anthropic"} else "https://fixture.test/v1"
    )
    await save(providers, provider, enabled=True, key="private-fixture-key", base_url=base)
    transport, _calls = fake_protocol(provider)

    def reply(request):
        if request.method == "GET":
            return transport.handler(request)
        return httpx.Response(200, json={"models": [], "error": "private-fixture-key"})

    providers.transport = httpx.MockTransport(reply)
    snapshot = await providers.test(provider)
    slot = next(slot for slot in snapshot["slots"] if slot["provider"] == provider)
    assert not slot["last_test"]["ok"]
    assert "protocol" in slot["last_test"]["error"].lower()
    assert "private-fixture-key" not in json.dumps(snapshot)


@pytest.mark.asyncio
async def test_key_clearing_cannot_resurrect_dotenv_aliases(providers):
    providers.store.env_path.write_text("GOOGLE_API_KEY=first-env\nGEMINI_API_KEY=alias-env\n")
    await save(providers, "google", key="", enabled=False)
    snapshot = await providers.store.snapshot_for_spawn()
    try:
        for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"):
            assert snapshot.environment[name] == ""
        assert not (await providers.read())["slots"][1]["configured"]
    finally:
        providers.store.cleanup_snapshot(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    [
        "https://user:secret@fixture.test",
        "https://fixture.test?key=secret",
        "http://192.168.1.1/v1",
        "https://fixture.test/private-fixture-key",
    ],
)
async def test_unsafe_urls_are_rejected_without_echoing_keys(providers, base):
    with pytest.raises(ConfigStoreError) as caught:
        await save(providers, base_url=base)
    assert "private-fixture-key" not in caught.value.detail


@pytest.mark.asyncio
async def test_saved_slots_update_readiness_and_protect_legacy_settings(providers, monkeypatch):
    from artemis.config.settings import settings

    monkeypatch.setattr(settings, "OPENAI_BASE_URL", settings.OPENAI_BASE_URL)

    await save(
        providers, key="new-saved-credential", base_url="https://saved.test/v1", enabled=True
    )
    await providers.activate_runtime()
    assert settings.get_api_key("openai").get_secret_value() == "new-saved-credential"
    snapshot = await providers.store.read()
    slot = next(slot for slot in snapshot["providers"] if slot["name"] == "openai")
    assert slot["key_preview"] == "****tial"
    assert slot["base_url"] == "https://saved.test/v1"
    assert "read-only" in slot["key_source"]
    with pytest.raises(ConfigStoreError) as caught:
        await providers.store.save(
            expected_version=snapshot["version"],
            default=snapshot["default"],
            credentials={"openai": "ignored-key"},
            base_urls={},
            actor_email="admin@example.com",
        )
    assert caught.value.code == "provider_managed"
    await save(providers, enabled=False)
    await providers.activate_runtime()
    assert not settings.get_api_key("openai")


@pytest.mark.asyncio
async def test_validation_errors_on_legacy_api_do_not_echo_credential_input(admin, caplog):
    async with admin:
        response = await admin.put(
            "/api/system/config",
            json={
                "version": "fixture",
                "default": {},
                "credentials": {"openai": {"key": "never-echo-credential"}},
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "credentials", "openai"]
    assert "never-echo-credential" not in response.text
    assert "never-echo-credential" not in caplog.text
