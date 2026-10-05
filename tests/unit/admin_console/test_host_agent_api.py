"""Computer registry and host-agent authentication (CHE-1095, release B1).

Server API seam: real HTTP/WebSocket requests against the app with a temp
SQLite file. No adb, device, browser or Cloudflare. Everything is behind
ARTEMIS_HOST_AGENT.
"""

from __future__ import annotations

import base64
import sqlite3
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from apps.admin_console.core import agent_auth
from apps.admin_console.routers import agent as agent_router
from apps.admin_console.core.access_control import AdminAPIError, admin_api_error_handler
from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_registry import host_registry

HOST = {"Host": "localhost"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _pub(key: Ed25519PrivateKey) -> str:
    return _b64(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "enabled")
    monkeypatch.setattr(host_registry, "db_path", tmp_path / "hosts.db")
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(host_registry, "clock", lambda: clock["now"])
    host_registry.reset_memory()
    yield clock
    host_registry.reset_memory()


@pytest.fixture
def clock(registry):
    return registry


@pytest.fixture
def admin():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000), base_url="http://localhost")


@pytest.fixture
def stranger():
    return TestClient(proxy_aware_app, client=("203.0.113.5", 50000), base_url="http://localhost")


def _new_code(admin) -> dict:
    response = admin.post("/api/hosts/enrollment-codes")
    assert response.status_code == 200, response.text
    return response.json()


def _enroll(client, code, key, name="Lab Mac", ip="198.51.100.7"):
    public = _pub(key)
    return client.post(
        "/api/agent/enroll",
        headers={"cf-connecting-ip": ip},
        json={
            "code": code,
            "public_key": public,
            "signature": _b64(key.sign(hr.enroll_message(code, public))),
            "name": name,
            "os": "macOS 15",
            "agent_version": "0.1.0",
            "protocol_version": hr.PROTOCOL_VERSION,
        },
    )


def _enrolled(admin, key=None, name="Lab Mac"):
    key = key or Ed25519PrivateKey.generate()
    code = _new_code(admin)["code"]
    response = _enroll(admin, code, key, name)
    assert response.status_code == 200, response.text
    return key, response.json()["host_id"]


def _hello(client, key, host_id, *, protocol=None, ts=None, nonce=None, audience=None):
    challenge = client.post("/api/agent/challenge", json={"host_id": host_id}).json()
    nonce = nonce or challenge["nonce"]
    audience = audience or challenge["audience"]
    protocol = protocol or hr.PROTOCOL_VERSION
    ts = ts if ts is not None else int(host_registry.clock())
    message = hr.connect_message(audience, protocol, host_id, nonce, ts)
    return {
        "type": "hello",
        "host_id": host_id,
        "nonce": nonce,
        "protocol_version": protocol,
        "timestamp": ts,
        "signature": _b64(key.sign(message)),
        "agent_version": "0.1.0",
    }


def _guarded_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(AdminAPIError, admin_api_error_handler)
    return app


def _handshake(client, key, host_id, **kw):
    """Open the socket; return (context to exit, ws, first server reply)."""
    context = client.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    ws.send_json(_hello(client, key, host_id, **kw))
    return context, ws, ws.receive_json()


# -- flag ---------------------------------------------------------------


def test_flag_off_hides_machine_routes_and_reports_disabled(admin, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "")
    body = admin.get("/api/hosts").json()
    assert body["enabled"] is False and body["hosts"] == []
    assert admin.post("/api/agent/challenge", json={"host_id": "x"}).status_code == 404
    assert admin.post("/api/hosts/enrollment-codes").status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect("/api/agent/connect", headers=HOST) as ws:
            ws.receive_json()


# -- human routes ---------------------------------------------------------


def test_only_admins_create_codes_revoke_and_rename(admin, stranger):
    assert stranger.post("/api/hosts/enrollment-codes").status_code in (401, 403)
    _key, host_id = _enrolled(admin)
    assert stranger.post(f"/api/hosts/{host_id}/revoke").status_code in (401, 403)
    assert stranger.post(f"/api/hosts/{host_id}/rename", json={"name": "x"}).status_code in (
        401,
        403,
    )
    assert admin.post(f"/api/hosts/{host_id}/rename", json={"name": "QA Linux"}).status_code == 200
    listed = stranger.get("/api/hosts").json()  # any user reads
    assert [h["name"] for h in listed["hosts"]] == ["QA Linux"]


def test_code_is_128_bit_hashed_at_rest_and_ttl_15_minutes(admin, tmp_path):
    created = _new_code(admin)
    assert len(base64.urlsafe_b64decode(created["code"] + "==")) == 16
    assert created["expires_at"] - 1_800_000_000 == 15 * 60
    raw = (
        sqlite3.connect(tmp_path / "hosts.db")
        .execute("SELECT * FROM host_enrollment_codes")
        .fetchall()
    )
    assert raw and created["code"] not in repr(raw)


# -- enrollment: bind, replay, rebind, rate limit ---------------------------


def test_enroll_binds_code_to_first_key_and_registers_computer(admin):
    key, host_id = _enrolled(admin, name="Lab Mac")
    host = admin.get("/api/hosts").json()["hosts"][0]
    assert host["id"] == host_id and host["name"] == "Lab Mac" and host["os"] == "macOS 15"
    assert (host["status"], host["reason"]) == ("offline", "never_connected")
    assert host["agent_version"] == "0.1.0" and host["protocol_version"] == 1
    assert "public_key" not in host


def test_same_key_retry_is_idempotent_within_ttl(admin, clock):
    key = Ed25519PrivateKey.generate()
    code = _new_code(admin)["code"]
    first = _enroll(admin, code, key)
    clock["now"] += 60
    second = _enroll(admin, code, key)
    assert first.status_code == second.status_code == 200
    assert first.json()["host_id"] == second.json()["host_id"]
    assert len(admin.get("/api/hosts").json()["hosts"]) == 1


def test_replayed_code_with_a_different_key_gets_used(admin):
    code = _new_code(admin)["code"]
    assert _enroll(admin, code, Ed25519PrivateKey.generate()).status_code == 200
    rebind = _enroll(admin, code, Ed25519PrivateKey.generate(), name="Intruder")
    assert rebind.status_code == 409 and rebind.json()["code"] == "code_used"
    assert [h["name"] for h in admin.get("/api/hosts").json()["hosts"]] == ["Lab Mac"]


def test_expired_and_unknown_codes_are_refused(admin, clock):
    code = _new_code(admin)["code"]
    clock["now"] += 15 * 60 + 1
    assert _enroll(admin, code, Ed25519PrivateKey.generate()).json()["code"] == "code_expired"
    unknown = _enroll(admin, "not-a-real-code", Ed25519PrivateKey.generate())
    assert unknown.status_code == 404 and unknown.json()["code"] == "code_invalid"


def test_enroll_requires_proof_of_possession_of_the_key(admin):
    code = _new_code(admin)["code"]
    victim = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    public = _pub(victim)
    response = admin.post(
        "/api/agent/enroll",
        json={
            "code": code,
            "public_key": public,
            "signature": _b64(attacker.sign(hr.enroll_message(code, public))),
            "name": "x",
            "os": "linux",
            "agent_version": "0.1.0",
            "protocol_version": 1,
        },
    )
    assert response.status_code == 401 and response.json()["code"] == "signature_invalid"
    # The failed proof must not burn or bind the code.
    assert _enroll(admin, code, Ed25519PrivateKey.generate()).status_code == 200


def test_enrollment_is_rate_limited_per_ip_and_per_code(admin):
    key = Ed25519PrivateKey.generate()
    statuses = [_enroll(admin, f"guess-{i}", key, ip="198.51.100.9").status_code for i in range(21)]
    assert statuses[:20] == [404] * 20 and statuses[20] == 429

    code = _new_code(admin)["code"]
    first = _enroll(admin, code, key, ip="198.51.100.20")
    assert first.status_code == 200
    seen = [
        _enroll(admin, code, Ed25519PrivateKey.generate(), ip=f"198.51.100.{30 + i}").status_code
        for i in range(12)
    ]
    assert seen[-1] == 429, seen


def test_cf_connecting_ip_is_only_trusted_from_the_local_cloudflared_peer(stranger):
    key = Ed25519PrivateKey.generate()
    # A remote peer cannot dodge the per-IP limit by rotating a spoofed header.
    codes = [_enroll(stranger, f"g{i}", key, ip=f"198.51.100.{i}").status_code for i in range(21)]
    assert codes[-1] == 429


# -- handshake: nonce, audience, downgrade, skew -----------------------------


def test_handshake_issues_a_24h_token_and_marks_the_computer_online(admin):
    key, host_id = _enrolled(admin)
    context, ws, reply = _handshake(admin, key, host_id)
    try:
        assert reply["type"] == "connected" and reply["generation"] == 1
        assert reply["expires_at"] - 1_800_000_000 == 24 * 3600
        host = admin.get("/api/hosts").json()["hosts"][0]
        assert host["status"] == "online" and host["generation"] == 1
    finally:
        context.__exit__(None, None, None)
    assert admin.get("/api/hosts").json()["hosts"][0]["status"] == "offline"


def test_challenge_nonce_is_single_use_and_expires_after_60_seconds(admin, clock):
    key, host_id = _enrolled(admin)
    context = admin.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    hello = _hello(admin, key, host_id)
    ws.send_json(hello)
    assert ws.receive_json()["type"] == "connected"
    context.__exit__(None, None, None)

    context = admin.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    ws.send_json(hello)  # replayed nonce and signature
    assert ws.receive_json()["code"] == "nonce_invalid"
    context.__exit__(None, None, None)

    stale = _hello(admin, key, host_id)
    clock["now"] += 61
    context = admin.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    ws.send_json(stale)
    assert ws.receive_json()["code"] == "nonce_invalid"
    context.__exit__(None, None, None)


def test_signature_binds_audience_protocol_host_and_timestamp(admin):
    key, host_id = _enrolled(admin)
    other_key, _other_id = _enrolled(admin, name="Other")
    context, ws, reply = _handshake(admin, key, host_id, audience="attacker.example")
    assert reply["code"] == "signature_invalid"
    context.__exit__(None, None, None)

    # Another computer's key cannot sign in as this host.
    context, ws, reply = _handshake(admin, other_key, host_id)
    assert reply["code"] == "signature_invalid"
    context.__exit__(None, None, None)

    # Tampering with the protocol after signing breaks the signature.
    hello = _hello(admin, key, host_id)
    hello["protocol_version"] = hr.PROTOCOL_VERSION + 5
    context = admin.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    ws.send_json(hello)
    assert ws.receive_json()["code"] == "signature_invalid"
    context.__exit__(None, None, None)


def test_clock_skew_over_60_seconds_is_rejected(admin):
    key, host_id = _enrolled(admin)
    context, _ws, reply = _handshake(admin, key, host_id, ts=int(host_registry.clock()) - 61)
    context.__exit__(None, None, None)
    assert reply["code"] == "clock_skew"


def test_protocol_below_min_supported_is_rejected_and_marks_update_required(admin, monkeypatch):
    key, host_id = _enrolled(admin)
    monkeypatch.setattr(hr, "MIN_SUPPORTED", 2)
    context, _ws, reply = _handshake(admin, key, host_id, protocol=1)
    context.__exit__(None, None, None)
    assert reply["code"] == "update_required" and reply["min_supported"] == 2
    host = admin.get("/api/hosts").json()["hosts"][0]
    assert (host["status"], host["reason"]) == ("update_required", "update_required")


def test_second_connection_for_a_host_replaces_the_first(admin):
    key, host_id = _enrolled(admin)
    first_context, first_ws, first = _handshake(admin, key, host_id)
    second_context, _second_ws, second = _handshake(admin, key, host_id)
    try:
        assert (first["generation"], second["generation"]) == (1, 2)
        with pytest.raises(WebSocketDisconnect) as closed:
            first_ws.receive_json()
        assert closed.value.code == 4409
        assert (
            admin.post(
                "/api/agent/renew", headers={"Authorization": f"Bearer {first['token']}"}
            ).status_code
            == 401
        )  # token was bound to generation 1
        assert (
            admin.post(
                "/api/agent/renew", headers={"Authorization": f"Bearer {second['token']}"}
            ).status_code
            == 200
        )
    finally:
        second_context.__exit__(None, None, None)
        first_context.__exit__(None, None, None)


def test_devices_report_counts_shared_and_unshared_phones(admin):
    key, host_id = _enrolled(admin)
    context, ws, _reply = _handshake(admin, key, host_id)
    try:
        ws.send_json(
            {
                "type": "devices",
                "devices": [
                    {"serial": "R5CT1", "model": "Pixel 8", "shared": True},
                    {"serial": "emulator-5554", "model": "AVD", "shared": False},
                ],
            }
        )
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        body = admin.get("/api/hosts").json()
        host = body["hosts"][0]
        assert (host["phones_shared"], host["phones_not_shared"]) == (1, 1)
        assert host["unshared_serials"] == ["emulator-5554"]
        assert host["share_command"] == "smartqa-host share <serial>"
        shared = [d for d in body["devices"] if d["source"] == "computer"]
        assert shared == [
            {
                "serial": "R5CT1",
                "model": "Pixel 8",
                "source": "computer",
                "computer_id": host_id,
                "computer_name": "Lab Mac",
                "computer_status": "online",
                "reason": None,
                "since": shared[0]["since"],
            }
        ]
    finally:
        context.__exit__(None, None, None)


# -- renewal and revoke --------------------------------------------------------


def test_token_renews_in_band_and_over_http(admin, clock):
    key, host_id = _enrolled(admin)
    context, ws, reply = _handshake(admin, key, host_id)
    try:
        clock["now"] += 3600
        ws.send_json({"type": "renew"})
        renewed = ws.receive_json()
        assert renewed["type"] == "renewed" and renewed["expires_at"] > reply["expires_at"]
        http = admin.post("/api/agent/renew", headers={"Authorization": f"Bearer {reply['token']}"})
        assert http.status_code == 200
    finally:
        context.__exit__(None, None, None)


def test_expired_or_garbage_tokens_cannot_renew(admin, clock):
    key, host_id = _enrolled(admin)
    context, _ws, reply = _handshake(admin, key, host_id)
    try:
        assert admin.post("/api/agent/renew").status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert admin.post("/api/agent/renew", headers=bad).status_code == 401
        clock["now"] += 24 * 3600 + 1
        good = {"Authorization": f"Bearer {reply['token']}"}
        assert admin.post("/api/agent/renew", headers=good).status_code == 401
    finally:
        context.__exit__(None, None, None)


def test_revoke_closes_the_live_socket_and_refuses_renewal_reconnect_and_upload(admin):
    key, host_id = _enrolled(admin)
    context, ws, reply = _handshake(admin, key, host_id)
    token = {"Authorization": f"Bearer {reply['token']}"}
    try:
        revoked = admin.post(f"/api/hosts/{host_id}/revoke")
        assert revoked.status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4410
    finally:
        context.__exit__(None, None, None)

    host = admin.get("/api/hosts").json()["hosts"][0]
    assert (host["status"], host["reason"]) == ("revoked", "revoked")
    assert admin.post("/api/agent/renew", headers=token).status_code == 401

    again, _ws, refusal = _handshake(admin, key, host_id)
    again.__exit__(None, None, None)
    assert refusal["code"] == "host_revoked"

    # The guard that upload finalization will sit behind refuses a revoked host.
    app = _guarded_app()

    @app.post("/finalize")
    async def finalize(host=Depends(agent_auth.require_agent_token("uploads"))):
        return {"host": host["id"]}

    assert TestClient(app).post("/finalize", headers=token).status_code == 401


def test_upload_scoped_guard_accepts_a_live_token(admin):
    key, host_id = _enrolled(admin)
    context, _ws, reply = _handshake(admin, key, host_id)
    try:
        app = _guarded_app()

        @app.post("/finalize")
        async def finalize(host=Depends(agent_auth.require_agent_token("uploads"))):
            return {"host": host["id"]}

        ok = TestClient(app).post(
            "/finalize", headers={"Authorization": f"Bearer {reply['token']}"}
        )
        assert ok.status_code == 200 and ok.json() == {"host": host_id}
    finally:
        context.__exit__(None, None, None)


def test_revoke_reports_how_many_runs_it_will_interrupt(admin):
    _key, host_id = _enrolled(admin)
    host = admin.get("/api/hosts").json()["hosts"][0]
    assert host["active_run_count"] == 0
    assert admin.post(f"/api/hosts/{host_id}/revoke").json()["interrupted_runs"] == 0


def test_registry_rows_start_offline_after_a_server_restart(admin):
    key, host_id = _enrolled(admin)
    context, _ws, _reply = _handshake(admin, key, host_id)
    host_registry.reset_for_boot()
    context.__exit__(None, None, None)
    host = admin.get("/api/hosts").json()["hosts"][0]
    assert (host["status"], host["reason"]) == ("offline", "server_restarted")


# -- installer / artifact gate ---------------------------------------------------


def test_installer_and_artifacts_need_a_valid_enrollment_code(admin):
    assert admin.get("/api/agent/install.sh").status_code == 401
    assert admin.get("/api/agent/dist/smartqa-host").status_code == 401
    code = _new_code(admin)["code"]
    ok = admin.get("/api/agent/install.sh", headers={"X-Artemis-Enrollment-Code": code})
    assert ok.status_code == 404 and ok.json()["code"] == "agent_artifact_unavailable"
    bad = admin.get("/api/agent/install.sh", headers={"X-Artemis-Enrollment-Code": "nope"})
    assert bad.status_code == 401


# -- polling the dialog ----------------------------------------------------------


def test_code_status_waits_for_an_authenticated_handshake_before_connected(admin, clock):
    created = _new_code(admin)
    path = f"/api/hosts/enrollment-codes/{created['code_id']}"
    assert admin.get(path).json()["status"] == "waiting"

    key = Ed25519PrivateKey.generate()
    host_id = _enroll(admin, created["code"], key, name="Desk PC").json()["host_id"]
    body = admin.get(path).json()  # enrolled, but nothing has authenticated yet
    assert (body["status"], body["computer_name"]) == ("enrolled", "Desk PC")

    context, _ws, reply = _handshake(admin, key, host_id)
    context.__exit__(None, None, None)
    assert reply["type"] == "connected"
    body = admin.get(path).json()
    assert (body["status"], body["computer_name"]) == ("connected", "Desk PC")

    other = _new_code(admin)
    clock["now"] += 15 * 60 + 1
    assert (
        admin.get(f"/api/hosts/enrollment-codes/{other['code_id']}").json()["status"] == "expired"
    )


def test_expired_session_stops_pong_and_device_publishing(admin, clock):
    key, host_id = _enrolled(admin)
    context, ws, reply = _handshake(admin, key, host_id)
    try:
        clock["now"] += 24 * 3600 + 1
        assert (
            admin.post(
                "/api/agent/renew", headers={"Authorization": f"Bearer {reply['token']}"}
            ).status_code
            == 401
        )
        ws.send_json({"type": "devices", "devices": [{"serial": "AFTER-EXPIRY", "shared": True}]})
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "error", "code": "auth_expired"}
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
    finally:
        context.__exit__(None, None, None)
    body = admin.get("/api/hosts").json()
    assert body["devices"] == []
    assert (body["hosts"][0]["status"], body["hosts"][0]["reason"]) == ("offline", "auth_expired")


def test_idle_socket_is_closed_when_its_token_expires(admin, clock, monkeypatch):
    monkeypatch.setattr(agent_router, "DEAD_AFTER_SECONDS", 1)
    key, host_id = _enrolled(admin)
    context, ws, _reply = _handshake(admin, key, host_id)
    try:
        clock["now"] += 24 * 3600 + 1  # the socket sends nothing at all
        assert ws.receive_json() == {"type": "error", "code": "auth_expired"}
    finally:
        context.__exit__(None, None, None)
    assert admin.get("/api/hosts").json()["hosts"][0]["reason"] == "auth_expired"


def test_http_renewal_moves_the_socket_deadline_without_spinning(admin, clock, monkeypatch):
    calls = {"n": 0}
    real = host_registry.validate_token

    def counting(token, scope):
        calls["n"] += 1
        return real(token, scope)

    monkeypatch.setattr(host_registry, "validate_token", counting)
    key, host_id = _enrolled(admin)
    context, ws, reply = _handshake(admin, key, host_id)
    try:
        clock["now"] += 3600
        auth = {"Authorization": f"Bearer {reply['token']}"}
        assert admin.post("/api/agent/renew", headers=auth).status_code == 200
        clock["now"] = reply["expires_at"] + 1  # past the original deadline, inside the renewed one
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        before = calls["n"]
        time.sleep(0.4)  # a stale deadline makes the handler re-validate in a tight loop
        assert calls["n"] - before < 10, calls["n"] - before
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        # Still ends at the renewed deadline.
        clock["now"] += 3600 + 1
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "error", "code": "auth_expired"}
    finally:
        clock["now"] += 10 * 3600  # releases a spinning handler so the test can exit
        context.__exit__(None, None, None)


def test_reenrolling_an_offline_computer_is_enrolled_until_a_new_handshake(admin):
    key, host_id = _enrolled(admin)
    context, _ws, first = _handshake(admin, key, host_id)
    context.__exit__(None, None, None)
    assert first["type"] == "connected"
    assert admin.get("/api/hosts").json()["hosts"][0]["status"] == "offline"

    second = _new_code(admin)
    assert _enroll(admin, second["code"], key).status_code == 200
    path = f"/api/hosts/enrollment-codes/{second['code_id']}"
    assert admin.get(path).json()["status"] == "enrolled"  # historical generation 1 must not count

    context, _ws, again = _handshake(admin, key, host_id)
    context.__exit__(None, None, None)
    assert again["generation"] == 2
    assert admin.get(path).json()["status"] == "connected"


def test_superseded_socket_cannot_publish_devices(admin):
    key, host_id = _enrolled(admin)
    first_context, first_ws, _first = _handshake(admin, key, host_id)
    second_context, second_ws, _second = _handshake(admin, key, host_id)
    try:
        with pytest.raises(WebSocketDisconnect) as closed:
            first_ws.receive_json()
        assert closed.value.code == 4409
        # A frame already in flight on the old socket arrives after the 4409 close.
        first_ws.send_json({"type": "devices", "devices": [{"serial": "STALE", "shared": True}]})
        time.sleep(0.3)
        second_ws.send_json({"type": "devices", "devices": [{"serial": "CURRENT", "shared": True}]})
        second_ws.send_json({"type": "ping"})
        assert second_ws.receive_json()["type"] == "pong"
        serials = [d["serial"] for d in admin.get("/api/hosts").json()["devices"]]
        assert serials == ["CURRENT"]
    finally:
        second_context.__exit__(None, None, None)
        first_context.__exit__(None, None, None)


def test_registry_refuses_device_writes_from_a_stale_generation_or_revoked_host(admin):
    key, host_id = _enrolled(admin)
    first_context, _ws1, first = _handshake(admin, key, host_id)
    second_context, _ws2, second = _handshake(admin, key, host_id)
    try:
        devices = [{"serial": "X1", "shared": True}]
        assert host_registry.set_devices(host_id, first["generation"], devices) is False
        assert host_registry.set_devices(host_id, second["generation"], devices) is True
        admin.post(f"/api/hosts/{host_id}/revoke")
        assert host_registry.set_devices(host_id, second["generation"], devices) is False
    finally:
        second_context.__exit__(None, None, None)
        first_context.__exit__(None, None, None)


def test_connect_message_golden_vector():
    message = hr.connect_message("lab.example", 1, "h1", "n1", 1800000000)
    assert message == b"artemis-host-connect/v1\nlab.example\n1\nh1\nn1\n1800000000"
    assert hr.enroll_message("c", "k") == b"artemis-host-enroll/v1\nc\nk"
