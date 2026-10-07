import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import jwt
import pytest

from apps.admin_console.core.access_control import (
    AccessConfig,
    AdminAPIError,
    CloudflareAccessVerifier,
    admin_api_error_handler,
    require_qa,
)
from apps.admin_console.core.preview_access import preview_access_verifier
from apps.admin_console.core.preview_fixtures import seed_preview_fixtures
from apps.admin_console.core.preview_fixtures import preview_owners
from apps.admin_console.core.preview_profile import prepare_preview_environment
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.routers import preview_synthetic, runs, sessions, system

QA1 = "qa1@example.test"
QA2 = "qa2@example.test"
ADMIN = "admin@example.test"
ISSUER = "https://preview.cloudflareaccess.com"
CONFIG = AccessConfig("cloudflare", "preview-audience", ISSUER, frozenset({ADMIN}))


@pytest.fixture
def signed_preview(tmp_path, monkeypatch):
    async def forbidden_fetch(_url):
        pytest.fail("A preview must never fetch JWKS over the network")

    monkeypatch.setattr(CloudflareAccessVerifier, "_http_fetch_jwks", forbidden_fetch)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="preview-key", alg="RS256", use="sig")
    bundle = tmp_path / "jwks.json"
    document = {"issuer": ISSUER, "fetched_at": time.time(), "keys": [public]}
    bundle.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_PREVIEW_JWKS_BUNDLE", str(bundle))
    monkeypatch.setenv("ARTEMIS_PREVIEW_QA_EMAILS", f"{QA1},{QA2}")
    verifier = preview_access_verifier(CONFIG)
    assert type(verifier) is CloudflareAccessVerifier
    root = tmp_path / "fixtures"
    root.mkdir()
    queue = seed_preview_fixtures(root, (QA1, QA2), ADMIN)
    traces = root / "traces"
    db = traces / "data_engine.db"
    monkeypatch.setattr(session_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", traces)
    monkeypatch.setattr(state, "queue_items", queue)
    monkeypatch.setattr(preview_synthetic.control, "paused", False)
    app = FastAPI(dependencies=[Depends(require_qa)])
    app.add_exception_handler(AdminAPIError, admin_api_error_handler)
    app.state.access_config = CONFIG
    app.state.access_verifier = verifier
    for router in (preview_synthetic.router, sessions.router, runs.router, system.router):
        app.include_router(router)
    client = TestClient(app, base_url="http://localhost")

    def headers(email=QA1, **claims):
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": CONFIG.audience,
                "sub": email,
                "email": email,
                "iat": now - 1,
                "nbf": now - 1,
                "exp": now + 300,
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": "preview-key"},
        )
        return {"Cf-Access-Jwt-Assertion": token}

    return client, headers, bundle, document, root, queue


def test_seed_is_deterministic_private_and_refuses_existing_data(tmp_path):
    snapshots = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        queue = seed_preview_fixtures(root, (QA1, QA2), ADMIN)
        with sqlite3.connect(root / "traces" / "data_engine.db") as connection:
            snapshots.append(
                connection.execute(
                    "SELECT s.session_id, s.initial_goal, s.start_time, s.end_time, s.status, "
                    "s.pid, m.requested_by FROM sessions s JOIN run_meta m USING(session_id) "
                    "ORDER BY s.session_id"
                ).fetchall()
            )
        assert len(queue) == 6
        assert all(item.get("device_id") is None for item in queue)
        before = (root / "traces" / "data_engine.db").read_bytes()
        with pytest.raises(ValueError, match="existing"):
            seed_preview_fixtures(root, (QA1, QA2), ADMIN)
        assert (root / "traces" / "data_engine.db").read_bytes() == before
    assert snapshots[0] == snapshots[1]
    assert len(snapshots[0]) == 9
    assert {row[-1] for row in snapshots[0]} == {QA1, QA2, ADMIN}
    assert {row[4] for row in snapshots[0]} == {"queued", "running", "completed"}
    assert all(row[5] is None for row in snapshots[0])


def test_preview_environment_never_adopts_live_paths(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    sentinel = live / "data_engine.db"
    sentinel.write_bytes(b"live data")
    environ = {
        "ARTEMIS_APP_DIR": str(tmp_path),
        "ARTEMIS_TRACES_DIR": str(live),
        "DATA_ENGINE_DB_PATH": str(sentinel),
        "TRACES_PATH": str(live),
    }
    root = prepare_preview_environment(environ)
    assert root.parent == tmp_path
    assert root.stat().st_mode & 0o777 == 0o700
    assert Path(environ["ARTEMIS_TRACES_DIR"]).is_relative_to(root)
    assert Path(environ["DATA_ENGINE_DB_PATH"]).is_relative_to(root)
    assert Path(environ["TRACES_PATH"]).is_relative_to(root)
    assert sentinel.read_bytes() == b"live data"


@pytest.mark.parametrize("environ", [{}, {"ARTEMIS_APP_DIR": "relative"}])
def test_preview_requires_an_explicit_absolute_fixture_parent(environ):
    with pytest.raises(ValueError, match="ARTEMIS_APP_DIR"):
        prepare_preview_environment(environ)


def test_signed_qa_admin_and_identity_changes_use_real_ownership(signed_preview):
    client, headers, *_rest = signed_preview
    for email in (QA1, QA2, ADMIN, QA1):
        response = client.get("/api/runs", headers=headers(email))
        assert response.status_code == 200
        assert len(response.json()["runs"]) == 3
        assert {run["requested_by"] for run in response.json()["runs"]} == {email}
        identity = client.get("/api/system/whoami", headers=headers(email)).json()
        assert identity["email"] == email
        assert identity["admin"] is (email == ADMIN)
    assert client.get("/api/runs?scope=all", headers=headers(QA1)).status_code == 403
    assert len(client.get("/api/runs?scope=all", headers=headers(ADMIN)).json()["runs"]) == 9


@pytest.mark.parametrize("operation", ["stop", "cancel", "delete"])
def test_foreign_owner_denials_leave_fixture_state_unchanged(signed_preview, operation):
    client, headers, _bundle, _document, _root, queue = signed_preview
    target = next(item for item in queue if item["requested_by"] == QA2)
    if operation == "stop":
        url = f"/api/stop?session_id={target['session_id']}&all=true"
    elif operation == "cancel":
        url = f"/api/tasks/{target['session_id']}/cancel-queued"
    else:
        url = f"/api/sessions/{target['session_id']}/delete"
    before = session_repo.get_session_by_id(target["session_id"])
    assert client.post(url, headers=headers(QA1)).status_code == 403
    assert session_repo.get_session_by_id(target["session_id"]) == before
    assert target["status"] == "pending"


def test_owner_delete_and_admin_delete_touch_only_synthetic_data(signed_preview):
    client, headers, *_rest = signed_preview
    own = next(
        run
        for run in client.get("/api/runs", headers=headers()).json()["runs"]
        if run["status"] == "completed"
    )
    url = f"/api/sessions/{own['session_id']}/delete"
    assert client.post(url, headers=headers(QA2)).status_code == 403
    assert client.post(url, headers=headers()).status_code == 200
    assert len(client.get("/api/runs", headers=headers()).json()["runs"]) == 2
    assert len(client.get("/api/runs", headers=headers(QA2)).json()["runs"]) == 3
    foreign = next(
        run
        for run in client.get("/api/runs", headers=headers(QA2)).json()["runs"]
        if run["status"] == "completed"
    )
    assert (
        client.post(f"/api/runs/{foreign['session_id']}/delete", headers=headers(ADMIN)).status_code
        == 200
    )


def test_owner_controls_keep_queue_and_fixture_database_consistent(signed_preview):
    client, headers, _bundle, _document, _root, queue = signed_preview
    pending = next(
        item for item in queue if item["requested_by"] == QA1 and item["status"] == "pending"
    )
    running = next(
        item for item in queue if item["requested_by"] == QA1 and item["status"] == "running"
    )
    assert (
        client.post(
            f"/api/tasks/{pending['session_id']}/cancel-queued", headers=headers()
        ).status_code
        == 200
    )
    assert pending["status"] == "cancelled"
    assert session_repo.get_session_by_id(pending["session_id"])["status"] == "cancelled"
    assert (
        client.post(f"/api/stop?session_id={running['session_id']}", headers=headers()).status_code
        == 200
    )
    assert running["status"] == "stopped"
    assert session_repo.get_session_by_id(running["session_id"])["status"] == "cancelled"
    assert (
        client.post(f"/api/sessions/{pending['session_id']}/delete", headers=headers()).status_code
        == 200
    )


def test_live_fixture_delete_and_foreign_resume_are_denied(signed_preview):
    client, headers, _bundle, _document, _root, queue = signed_preview
    running = next(
        item for item in queue if item["requested_by"] == QA1 and item["status"] == "running"
    )
    assert (
        client.post(f"/api/sessions/{running['session_id']}/delete", headers=headers()).status_code
        == 409
    )
    preview_synthetic.control.paused = True
    assert client.post("/api/resume", headers=headers()).status_code == 403
    assert preview_synthetic.control.paused is True
    assert client.post("/api/resume", headers=headers(ADMIN)).json() == {"status": "resumed"}


@pytest.mark.parametrize("operation", ["stop", "cancel"])
def test_persistence_failure_does_not_claim_or_apply_a_successful_control(
    signed_preview, monkeypatch, operation
):
    client, headers, _bundle, _document, _root, queue = signed_preview
    target = next(item for item in queue if item["requested_by"] == QA1)
    before = dict(target)
    monkeypatch.setattr(session_repo, "update_session_status", lambda *_args: False)
    if operation == "stop":
        url = f"/api/stop?session_id={target['session_id']}&all=true"
    else:
        url = f"/api/tasks/{target['session_id']}/cancel-queued"
    assert client.post(url, headers=headers()).status_code == 503
    assert target == before
    assert session_repo.get_session_by_id(target["session_id"])["status"] == "queued"


@pytest.mark.parametrize("attack", ["signature", "hs256", "unknown-key"])
def test_plausible_but_forged_jwts_are_denied(signed_preview, attack):
    client, headers, *_rest = signed_preview
    token = headers()["Cf-Access-Jwt-Assertion"]
    if attack == "signature":
        unsigned = jwt.decode(token, options={"verify_signature": False})
        unsigned["email"] = ADMIN
        encoded = jwt.api_jws.base64url_encode(json.dumps(unsigned).encode()).decode()
        token = token.split(".")[0] + "." + encoded + "." + token.split(".")[2]
    elif attack == "hs256":
        token = jwt.encode(
            jwt.decode(token, options={"verify_signature": False}),
            "a-long-synthetic-secret-not-an-rsa-key",
            algorithm="HS256",
            headers={"kid": "preview-key"},
        )
    else:
        header = jwt.api_jws.base64url_encode(b'{"alg":"RS256","kid":"unknown-key"}').decode()
        token = header + "." + ".".join(token.split(".")[1:])
    assert client.get("/api/runs", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 401


def test_unsigned_identity_header_cannot_change_the_signed_identity(signed_preview):
    client, headers, *_rest = signed_preview
    request_headers = {**headers(), "Cf-Access-Authenticated-User-Email": ADMIN}
    identity = client.get("/api/system/whoami", headers=request_headers).json()
    assert identity["email"] == QA1
    assert identity["admin"] is False


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": 1},
        {"aud": "foreign-app"},
        {"iss": "https://foreign.cloudflareaccess.com"},
        {"nbf": 1_900_000_000},
        {"iat": 1_900_000_000},
        {"email": ""},
        {"sub": None},
    ],
)
def test_signed_invalid_claims_are_denied(signed_preview, claims):
    client, headers, *_rest = signed_preview
    assert client.get("/api/runs", headers=headers(**claims)).status_code == 401


@pytest.mark.parametrize("token", [None, "qa1@example.test", "eyJhbGciOiJub25lIn0.e30."])
def test_missing_forged_and_unsigned_tokens_are_denied(signed_preview, token):
    client, *_rest = signed_preview
    headers = {} if token is None else {"Cf-Access-Jwt-Assertion": token}
    headers["Cf-Access-Authenticated-User-Email"] = QA1
    assert client.get("/api/runs", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        "stale",
        "missing",
        "unknown",
        "issuer",
        "private",
        "future",
        "boolean",
        "nan",
        "huge-timestamp",
        "modulus",
        "empty",
        "duplicate",
        "kty",
        "alg",
        "malformed",
        "oversize",
        "symlink",
        "fifo",
    ],
)
def test_bundle_changes_deny_even_after_a_successful_cached_identity(signed_preview, change):
    client, headers, bundle, document, *_rest = signed_preview
    assert client.get("/api/runs", headers=headers()).status_code == 200
    if change == "missing":
        bundle.unlink()
    elif change == "symlink":
        target = bundle.with_suffix(".target")
        bundle.rename(target)
        bundle.symlink_to(target)
    elif change == "fifo":
        bundle.unlink()
        os.mkfifo(bundle)
    elif change == "malformed":
        bundle.write_text("{", encoding="utf-8")
    elif change == "oversize":
        bundle.write_text(" " * 65537, encoding="utf-8")
    else:
        if change == "stale":
            document["fetched_at"] = time.time() - 3601
        elif change == "unknown":
            document["keys"][0]["kid"] = "rotated-key"
        elif change == "issuer":
            document["issuer"] = "https://foreign.cloudflareaccess.com"
        elif change == "private":
            document["keys"][0]["d"] = "private-key-material"
        elif change == "future":
            document["fetched_at"] = time.time() + 300
        elif change == "boolean":
            document["fetched_at"] = True
        elif change == "nan":
            document["fetched_at"] = float("nan")
        elif change == "huge-timestamp":
            document["fetched_at"] = 10**500
        elif change == "modulus":
            document["keys"][0].pop("n")
        elif change == "empty":
            document["keys"] = []
        elif change == "duplicate":
            document["keys"].append(document["keys"][0])
        elif change == "kty":
            document["keys"][0]["kty"] = "oct"
        elif change == "alg":
            document["keys"][0]["alg"] = "HS256"
        bundle.write_text(json.dumps(document), encoding="utf-8")
    assert client.get("/api/runs", headers=headers()).status_code == 401


def test_preview_verifier_rejects_open_mode_and_missing_bundle(monkeypatch):
    with pytest.raises(ValueError, match="cloudflare"):
        preview_access_verifier(AccessConfig())
    monkeypatch.delenv("ARTEMIS_PREVIEW_JWKS_BUNDLE", raising=False)
    with pytest.raises(ValueError, match="ARTEMIS_PREVIEW_JWKS_BUNDLE"):
        preview_access_verifier(CONFIG)


def test_fresh_key_rotation_accepts_the_new_signed_identity_and_rejects_the_old_key(signed_preview):
    client, headers, bundle, document, *_rest = signed_preview
    old_headers = headers()
    assert client.get("/api/runs", headers=old_headers).status_code == 200
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public.update(kid="new-key", alg="RS256", use="sig")
    document.update(keys=[public], fetched_at=time.time())
    bundle.write_text(json.dumps(document), encoding="utf-8")
    claims = jwt.decode(old_headers["Cf-Access-Jwt-Assertion"], options={"verify_signature": False})
    claims.update(email=QA2, sub=QA2)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "new-key"})
    response = client.get("/api/runs", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 200
    assert {run["requested_by"] for run in response.json()["runs"]} == {QA2}
    assert client.get("/api/runs", headers=old_headers).status_code == 401


@pytest.mark.parametrize(
    "owners", ["", QA1, f"{QA1},{QA1}", f"{QA1},{ADMIN}", "invalid,qa2@example.test"]
)
def test_fixture_owner_configuration_fails_closed(monkeypatch, owners):
    monkeypatch.setenv("ARTEMIS_PREVIEW_QA_EMAILS", owners)
    monkeypatch.setenv("ARTEMIS_ADMIN_EMAILS", ADMIN)
    with pytest.raises(ValueError):
        preview_owners(CONFIG)


def test_fixture_admin_must_be_explicit(monkeypatch):
    monkeypatch.setenv("ARTEMIS_PREVIEW_QA_EMAILS", f"{QA1},{QA2}")
    monkeypatch.delenv("ARTEMIS_ADMIN_EMAILS", raising=False)
    with pytest.raises(ValueError, match="explicit"):
        preview_owners(CONFIG)


_BOOT_PROBE = textwrap.dedent("""
    import json, os, socket, subprocess
    effects = []
    def forbidden(*args, **kwargs):
        effects.append("network-or-process")
        raise AssertionError("Preview attempted a network or process call")
    socket.socket.connect = forbidden
    class ForbiddenProcess(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            forbidden()
    subprocess.Popen = ForbiddenProcess
    import apps.admin_console.server as server
    from fastapi.testclient import TestClient
    with TestClient(server.app, base_url="http://localhost") as client:
        headers = {"Cf-Access-Jwt-Assertion": os.environ["ARTEMIS_TEST_ACCESS_TOKEN"]}
        all_runs = client.get("/api/runs?scope=all", headers=headers)
        assert all_runs.status_code == 200, all_runs.text
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/devices", headers=headers).json() == {"devices": []}
        print(json.dumps({"runs": all_runs.json()["runs"], "root": str(server.PREVIEW_ROOT),
                          "effects": effects, "worker": server.state.worker_task,
                          "retention": server.state.retention_task}))
""")


def test_real_preview_boot_seeds_private_fixtures_without_process_or_network_calls(
    tmp_path, preview_access_env
):
    live = tmp_path / "live"
    live.mkdir()
    sentinel = live / "data_engine.db"
    sentinel.write_bytes(b"must not be opened as sqlite")
    fixture_parent = tmp_path / "app"
    fixture_parent.mkdir()
    (fixture_parent / ".env").write_text("ARTEMIS_AUTH_MODE=open\n", encoding="utf-8")
    environment = {
        **os.environ,
        **preview_access_env,
        "ARTEMIS_PREVIEW_PROFILE": "1",
        "ARTEMIS_APP_DIR": str(fixture_parent),
        "ARTEMIS_TRACES_DIR": str(live),
        "TRACES_PATH": str(live),
        "DATA_ENGINE_DB_PATH": str(sentinel),
        "TMPDIR": str(tmp_path),
        "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
    }
    result = subprocess.run(
        [sys.executable, "-c", _BOOT_PROBE],
        cwd=Path(__file__).resolve().parents[3],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(outcome["runs"]) == 9
    assert outcome["effects"] == []
    assert outcome["worker"] is None and outcome["retention"] is None
    assert Path(outcome["root"]).parent == fixture_parent
    assert sentinel.read_bytes() == b"must not be opened as sqlite"
