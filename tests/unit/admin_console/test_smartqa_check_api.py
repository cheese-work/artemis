from __future__ import annotations

import hashlib
import json
import sqlite3
from urllib.parse import urlencode

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi.testclient import TestClient
from PIL import Image

from apps.admin_console.core.access_control import (
    AccessConfig,
    CloudflareAccessVerifier,
    _service_route_allowed,
    _service_principals_from_environment,
)

CLIENT_ID = "agent-client.access"
OTHER_CLIENT_ID = "unmapped-client.access"
OTHER_QA_ID = "other-qa.access"
SERVICE_OWNER = "service:" + hashlib.sha256(b"smartqa-agent").hexdigest()
APK = b"PK\x03\x04" + b"apk-bytes" * 10
APK_SHA = hashlib.sha256(APK).hexdigest()
PRINCIPALS = json.dumps(
    {
        CLIENT_ID: {
            "name": "smartqa-agent",
            "routes": [
                "POST /api/smartqa/apks",
                "GET /api/sessions/{session_id}",
                "GET /api/smartqa/report",
                "POST /api/stop",
            ],
        },
        OTHER_QA_ID: {
            "name": "other-qa",
            "routes": [
                "GET /api/smartqa/report",
                "GET /api/sessions/{session_id}",
                "POST /api/stop",
            ],
        },
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
    from apps.admin_console.routers import sessions, smartqa_check
    from apps.admin_console.server import app

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-key"
    monkeypatch.setenv("ARTEMIS_SERVICE_PRINCIPALS", PRINCIPALS)
    monkeypatch.setenv("ARTEMIS_SMARTQA_CHECK", "1")
    monkeypatch.setenv("ARTEMIS_SMARTQA_APK_DIR", str(tmp_path / "apks"))
    monkeypatch.setattr(smartqa_check, "TRACES_PATH", library.traces)
    monkeypatch.setattr(sessions.session_repo, "db_path", library.db)
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
    assert _service_route_allowed("GET", "/api/sessions/abc-123", routes)
    assert not _service_route_allowed("GET", "/api/sessions/abc/steps", routes)
    assert not _service_route_allowed("GET", "/api/sessions/", routes)
    assert not _service_route_allowed("POST", "/api/sessions/abc-123", routes)


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


@pytest.fixture
def owned_run(library):
    def seed(**kwargs):
        session_id = library.seed(**kwargs)
        with sqlite3.connect(library.db) as connection:
            connection.execute(
                "UPDATE run_meta SET requested_by=? WHERE session_id=?", (SERVICE_OWNER, session_id)
            )
        return session_id

    return seed


def test_service_polls_only_its_live_runs(call, library, owned_run):
    session_id = owned_run(status="running")
    path = f"/api/sessions/{session_id}"
    assert call("GET", path).json()["status"] == "running"
    assert call("GET", path, client_id=OTHER_QA_ID).status_code == 403
    with sqlite3.connect(library.db) as connection:
        connection.execute("UPDATE run_meta SET deleted_at=1 WHERE session_id=?", (session_id,))
    assert call("GET", path).status_code == 404


def test_service_can_stop_its_run_by_id_only(call, monkeypatch, owned_run):
    from apps.admin_console.routers import tasks

    stopped = []

    def stop_tasks(**kwargs):
        stopped.append(kwargs["session_id"])
        return True

    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", stop_tasks)
    session_id = owned_run(status="running")
    target = {"session_id": session_id}
    assert call("POST", "/api/stop", json=target, client_id=OTHER_QA_ID).status_code == 403
    assert stopped == []
    assert call("POST", "/api/stop", json=target).json()["status"] == "stopped"
    assert stopped == [session_id]


@pytest.fixture
def report_path(library, owned_run):
    def seed(*, crash=False, dark_sha=APK_SHA, outside_png=False):
        ids = []
        for theme in ("light", "dark"):
            session_id = owned_run()
            ids.append(session_id)
            run = {
                "installed_apk_sha256": APK_SHA if theme == "light" else dark_sha,
                "steps": [
                    {
                        "step": 1,
                        "theme": theme,
                        "timestamp": "fixture",
                        "screen_before": "home",
                        "screen_after": "home",
                        "action": {"type": "tap"},
                        "before_png": "../snap.png" if outside_png else "snap.png",
                        "after_png": "snap.png",
                    }
                ],
                "logcat_crash": (
                    "FATAL EXCEPTION: main\nProcess: com.example.sample, PID: 42"
                    if crash and theme == "dark"
                    else ""
                ),
            }
            evidence = library.write(
                session_id,
                "smartqa/session.json",
                json.dumps(
                    {
                        "package": "com.example.sample",
                        "declared_screens": [{"id": "home"}],
                        "themes": {theme: run},
                    }
                ),
            )
            image = Image.new("RGB", (4, 4))
            image.putpixel((0, 0), (255, 255, 255))
            image.save(evidence.with_name("snap.png"))
            image.save(evidence.parent.parent / "snap.png")
        return "/api/smartqa/report?" + urlencode(
            {"apk_sha256": APK_SHA, "session_id": ids}, doseq=True
        )

    return seed


@pytest.mark.parametrize("crash", [False, True])
def test_report_evaluates_both_themes_for_the_installed_apk(call, report_path, crash):
    response = call("GET", report_path(crash=crash))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["apk_sha256"] == APK_SHA
    assert body["green"] is not crash
    assert body["gate"]["rules"][0]["passed"] is not crash
    assert set(body["themes"]) == {"light", "dark"}


def test_report_refuses_mismatched_installed_digest(call, report_path):
    response = call("GET", report_path(dark_sha="0" * 64))
    assert response.status_code == 409
    assert response.json()["code"] == "report_apk_mismatch"


def test_report_link_requires_run_access(call, report_path):
    path = report_path()
    assert call("GET", path, token=False).status_code == 401
    assert call("GET", path, client_id=OTHER_QA_ID).status_code == 403


def test_report_flag_off_hides_the_report(call, report_path, monkeypatch):
    monkeypatch.delenv("ARTEMIS_SMARTQA_CHECK")
    assert call("GET", report_path()).status_code == 404


def test_report_does_not_read_capture_outside_its_theme(call, report_path):
    response = call("GET", report_path(outside_png=True))
    assert response.status_code == 422
    assert response.json()["code"] == "report_evidence_invalid"
