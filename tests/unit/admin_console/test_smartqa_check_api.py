from __future__ import annotations

import hashlib
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi.testclient import TestClient

from apps.admin_console.core.access_control import (
    AccessConfig,
    CloudflareAccessVerifier,
    _route_allowed,
    _service_principals_from_environment,
)

CLIENT_ID = "agent-client.access"
OTHER_CLIENT_ID = "unmapped-client.access"
APK = b"PK\x03\x04" + b"apk-bytes" * 10
APK_SHA = hashlib.sha256(APK).hexdigest()
PRINCIPALS = json.dumps(
    {
        CLIENT_ID: {
            "name": "smartqa-agent",
            "routes": ["POST /api/smartqa/apks", "GET /api/sessions/{session_id}"],
        }
    }
)


def _service_token(private_key: bytes, client_id: str) -> str:
    claims = {
        "type": "app",
        "iss": "https://team.cloudflareaccess.com",
        "aud": ["app-audience"],
        "sub": "",
        "common_name": client_id,
        "iat": 1_790_000_000,
        "exp": 1_900_000_000,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def call(monkeypatch, tmp_path, library):
    from apps.admin_console.server import app

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-key"
    monkeypatch.setenv("ARTEMIS_SERVICE_PRINCIPALS", PRINCIPALS)
    monkeypatch.setenv("ARTEMIS_SMARTQA_CHECK", "1")
    monkeypatch.setenv("ARTEMIS_SMARTQA_APK_DIR", str(tmp_path / "apks"))
    monkeypatch.setattr(
        app.state,
        "access_config",
        AccessConfig(
            auth_mode="cloudflare",
            audience="app-audience",
            issuer="https://team.cloudflareaccess.com",
            service_principals=_service_principals_from_environment(),
        ),
    )
    monkeypatch.setattr(
        app.state,
        "access_verifier",
        CloudflareAccessVerifier(fetch_jwks=lambda _url: {"keys": [jwk]}),
    )
    client = TestClient(app, base_url="http://localhost")

    def send(method, path, client_id=CLIENT_ID, token=True, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if token:
            headers["Cf-Access-Jwt-Assertion"] = _service_token(pem, client_id)
        return client.request(method, path, headers=headers, **kwargs)

    send.apk_dir = tmp_path / "apks"
    return send


def test_upload_without_a_token_is_refused(call):
    response = call("POST", "/api/smartqa/apks", token=False, content=APK)
    assert response.status_code in {401, 403}
    assert not call.apk_dir.exists()


def test_upload_with_an_unmapped_token_is_refused(call):
    response = call("POST", "/api/smartqa/apks", client_id=OTHER_CLIENT_ID, content=APK)
    assert response.status_code == 403
    assert not call.apk_dir.exists()


def test_mapped_principal_uploads_and_gets_the_sha_back(call):
    response = call("POST", "/api/smartqa/apks", content=APK, headers={"X-Content-SHA256": APK_SHA})
    assert response.status_code == 200
    body = response.json()
    assert body["sha256"] == APK_SHA and body["size_bytes"] == len(APK)
    assert (call.apk_dir / f"{APK_SHA}.apk").read_bytes() == APK
    assert [p.name for p in call.apk_dir.iterdir()] == [f"{APK_SHA}.apk"]  # no temp file left


def test_sha_mismatch_is_rejected_and_leaves_nothing(call):
    response = call(
        "POST", "/api/smartqa/apks", content=APK, headers={"X-Content-SHA256": "0" * 64}
    )
    assert response.status_code == 422
    assert list(call.apk_dir.iterdir()) == []


def test_oversize_upload_is_rejected_and_leaves_nothing(call, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_APK_MAX_BYTES", "16")
    response = call("POST", "/api/smartqa/apks", content=APK)
    assert response.status_code == 413
    assert not call.apk_dir.exists()  # refused from Content-Length before any write


def test_non_apk_bytes_are_rejected(call):
    response = call("POST", "/api/smartqa/apks", content=b"not a zip file at all")
    assert response.status_code == 415
    assert list(call.apk_dir.iterdir()) == []


def test_flag_off_hides_the_endpoint(call, monkeypatch):
    monkeypatch.delenv("ARTEMIS_SMARTQA_CHECK")
    assert call("POST", "/api/smartqa/apks", content=APK).status_code == 404


def test_principal_route_templates_match_one_segment_only():
    routes = frozenset({("GET", "/api/sessions/{session_id}")})
    assert _route_allowed(routes, "GET", "/api/sessions/abc-123")
    assert not _route_allowed(routes, "GET", "/api/sessions/abc/steps")
    assert not _route_allowed(routes, "GET", "/api/sessions/")
    assert not _route_allowed(routes, "POST", "/api/sessions/abc-123")


def test_template_route_reaches_the_session_but_not_other_paths(call):
    reachable = call("GET", "/api/sessions/00000000-0000-4000-8000-000000000000")
    assert reachable.json().get("code") != "service_route_denied"
    denied = call("GET", "/api/system/failures")
    assert denied.status_code == 403 and denied.json()["code"] == "service_route_denied"


def test_oversize_chunked_upload_without_content_length_is_cut_off(call, monkeypatch):
    monkeypatch.setenv("ARTEMIS_SMARTQA_APK_MAX_BYTES", "16")
    response = call("POST", "/api/smartqa/apks", content=iter([APK[:10], APK[10:]]))
    assert response.status_code == 413
    assert list(call.apk_dir.iterdir()) == []
