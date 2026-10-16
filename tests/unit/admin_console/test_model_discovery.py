import io
import json

import httpx
import pytest

from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.services import config_store as config_module
from apps.admin_console.services.config_store import ConfigStore
from apps.admin_console.services.model_discovery import ModelDiscovery, parse_models
from artemis.utils.file import load_jsonc


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "SERVICE_ENVIRONMENT_KEYS", frozenset())
    config = tmp_path / "artemis.jsonc"
    config.write_text(
        json.dumps(
            {
                "default": {
                    "provider": "openai",
                    "model": "default-model",
                    "fallback": {"provider": "openai", "model": "default-model"},
                },
                "nodes": {},
            }
        )
    )
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=secret-do-not-return\nOPENAI_BASE_URL=https://proxy.test/v1/\n")
    return ConfigStore(config, env)


@pytest.mark.parametrize(
    "provider,payload,expected",
    [
        (
            "openai",
            {"data": [{"id": "gemini-via-proxy"}, {"id": "gpt-model"}, {"id": "gpt-model"}]},
            ["gemini-via-proxy", "gpt-model"],
        ),
        ("openrouter", {"data": [{"id": "vendor/model"}]}, ["vendor/model"]),
        ("xai", {"data": [{"id": "grok-model"}]}, ["grok-model"]),
        ("anthropic", {"data": [{"id": "claude-model"}]}, ["claude-model"]),
        (
            "google",
            {
                "models": [
                    {
                        "name": "models/gemini-model",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {"name": "models/embed", "supportedGenerationMethods": ["embedContent"]},
                ]
            },
            ["gemini-model"],
        ),
    ],
)
def test_parses_provider_model_lists(provider, payload, expected):
    assert parse_models(provider, payload) == expected


@pytest.mark.parametrize(
    "payload", [{}, {"data": "bad"}, {"data": [{}]}, {"data": [{"id": "\nunsafe"}]}]
)
def test_malformed_discovery_is_not_a_good_list(payload):
    with pytest.raises(ValueError):
        parse_models("openai", payload)


@pytest.mark.asyncio
async def test_scans_keys_caches_and_never_returns_secrets(store):
    calls = []

    def reply(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer secret-do-not-return"
        return httpx.Response(
            200,
            json={
                "data": [{"id": "gpt-model"}, {"id": "codex-auto-review"}, {"id": "gpt-reserve"}]
            },
        )

    discovery = ModelDiscovery(store, transport=httpx.MockTransport(reply))
    first = await discovery.catalog()
    assert first == await discovery.catalog()
    assert len(calls) == 1
    assert str(calls[0].url) == "https://proxy.test/v1/models"
    assert first["models"] == [
        {"provider": "openai", "model": "gpt-model", "source": "https://proxy.test/v1/models"}
    ]
    assert first["default"] == {"provider": "openai", "model": "default-model"}
    assert "secret-do-not-return" not in json.dumps(first)


@pytest.mark.asyncio
async def test_configurable_deny_list_is_applied_even_to_cached_rows(store):
    discovery = ModelDiscovery(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"data": [{"id": "gpt-model"}, {"id": "codex-auto-review"}]}
            )
        ),
    )
    assert len((await discovery.catalog())["models"]) == 1
    config = json.loads(store.config_path.read_text())
    config["model_discovery"] = {"deny": ["openai/gpt-model"]}
    store.config_path.write_text(json.dumps(config))
    assert [row["model"] for row in (await discovery.catalog())["models"]] == ["codex-auto-review"]


@pytest.mark.asyncio
async def test_failure_keeps_last_good_list_and_redacts_provider_error(store, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("apps.admin_console.services.provider_settings.time.time", lambda: now[0])
    failed = [False]

    def reply(request):
        return (
            httpx.Response(403, text="secret-do-not-return")
            if failed[0]
            else httpx.Response(200, json={"data": [{"id": "gpt-model"}]})
        )

    discovery = ModelDiscovery(store, transport=httpx.MockTransport(reply))
    original = await discovery.catalog()
    failed[0] = True
    now[0] = 601
    stale = await discovery.catalog()
    assert stale["models"] == original["models"]
    assert len(stale["errors"]) == 1
    assert stale["errors"][0]["provider"] == "openai"
    assert "HTTP 403" in stale["errors"][0]["error"]
    assert "secret-do-not-return" not in json.dumps(stale)


@pytest.mark.asyncio
async def test_expiry_discovers_new_proxy_models_and_removed_key_revokes_rows(store, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("apps.admin_console.services.provider_settings.time.time", lambda: now[0])
    models = [{"id": "gpt-model"}]
    discovery = ModelDiscovery(
        store, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": models}))
    )
    await discovery.catalog()
    models.append({"id": "gemini-new"})
    now[0] = 601
    assert len((await discovery.catalog())["models"]) == 2
    store.env_path.write_text("")
    assert (await discovery.catalog())["models"] == []


@pytest.mark.asyncio
async def test_validation_includes_the_default_model(store):
    discovery = ModelDiscovery(
        store,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": "gpt-model"}]})
        ),
    )
    with pytest.raises(AdminAPIError):
        await discovery.resolve(None)
    assert await discovery.resolve({"provider": "openai", "model": "gpt-model"}) == {
        "provider": "openai",
        "model": "gpt-model",
    }
    for selection in [
        {"provider": "google", "model": "gpt-model"},
        {"provider": "openai", "model": "free-text"},
        {"provider": "openai", "model": "default-model"},
    ]:
        with pytest.raises(AdminAPIError) as caught:
            await discovery.resolve(selection)
        assert caught.value.status_code == 400
    store.env_path.write_text("")
    with pytest.raises(AdminAPIError):
        await discovery.resolve(None)


@pytest.mark.asyncio
async def test_each_key_uses_its_provider_endpoint_and_pagination(store):
    store.env_path.write_text(
        "GEMINI_API_KEY=google-secret\nANTHROPIC_API_KEY=anthropic-secret\nOPEN_ROUTER_API_KEY=router-secret\nXAI_API_KEY=xai-secret\n"
    )
    calls = []

    def reply(request):
        calls.append(request)
        if "googleapis" in request.url.host:
            assert request.headers["x-goog-api-key"] == "google-secret"
            page = request.url.params.get("pageToken")
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-" + (page or "first"),
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ],
                    **({} if page else {"nextPageToken": "second"}),
                },
            )
        if "anthropic" in request.url.host:
            assert request.headers["x-api-key"] == "anthropic-secret"
            page = request.url.params.get("after_id")
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-" + (page or "first")}],
                    "has_more": not page,
                    "last_id": "second",
                },
            )
        return httpx.Response(200, json={"data": [{"id": "chat-model"}]})

    result = await ModelDiscovery(store, transport=httpx.MockTransport(reply)).catalog()
    assert len(calls) == 6
    assert {row["provider"] for row in result["models"]} == {
        "google",
        "anthropic",
        "openrouter",
        "xai",
    }
    assert not result["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "key_name", "base_name", "api_version"),
    [
        ("google", "GOOGLE_API_KEY", "GOOGLE_BASE_URL", "v1beta"),
        ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "v1"),
    ],
)
async def test_native_base_override_matches_worker_sdk_versioned_paths(
    store, provider, key_name, base_name, api_version
):
    store.env_path.write_text(f"{key_name}=test-key\n{base_name}=https://native.test/prefix/\n")

    def reply(request):
        assert request.url.path == f"/prefix/{api_version}/models"
        payload = (
            {
                "models": [
                    {"name": "models/native", "supportedGenerationMethods": ["generateContent"]}
                ]
            }
            if provider == "google"
            else {"data": [{"id": "native"}]}
        )
        return httpx.Response(200, json=payload)

    result = await ModelDiscovery(store, transport=httpx.MockTransport(reply)).catalog()
    assert not result["errors"]
    assert result["models"][0]["model"] == "native"


@pytest.mark.asyncio
async def test_worker_override_is_private_and_does_not_change_server_default(store):
    before = store.config_path.read_bytes()
    selected = {"provider": "google", "model": "gemini-model"}
    snapshot = await store.snapshot_for_spawn(operator_model=selected)
    try:
        config = load_jsonc(io.StringIO(snapshot.config_path.read_text()))
        assert config["nodes"]["operator"]["model"] == "gemini-model"
        assert config["nodes"]["operator"]["provider"] == "google"
        assert config["nodes"]["operator"]["fallback"] == selected
        assert config["nodes"]["operator"]["fallback_enabled"] is False
        assert config["default"]["model"] == "default-model"
        assert store.config_path.read_bytes() == before
    finally:
        snapshot.config_path.unlink()


@pytest.mark.asyncio
async def test_explicit_default_choice_disables_fallback_but_implicit_default_is_unchanged(store):
    before = store.config_path.read_bytes()
    implicit = await store.snapshot_for_spawn()
    explicit = await store.snapshot_for_spawn(
        operator_model={"provider": "openai", "model": "default-model"}
    )
    try:
        assert implicit.config_path.read_bytes() == before
        config = load_jsonc(io.StringIO(explicit.config_path.read_text()))
        assert config["nodes"]["operator"]["fallback_enabled"] is False
        assert config["nodes"]["operator"]["fallback"]["model"] == "default-model"
        assert store.config_path.read_bytes() == before
    finally:
        store.cleanup_snapshot(implicit)
        store.cleanup_snapshot(explicit)
