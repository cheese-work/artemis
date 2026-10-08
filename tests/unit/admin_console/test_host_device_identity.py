"""Opaque device ids, org pepper and duplicate-device flags (CHE-1129, B3a-2).

Server API seam: real HTTP/WebSocket requests against the app with a temp
SQLite file. The agent presents only "sd-" + 16 hex HMAC ids; no raw serial
may reach the registry, an API payload, an audit event or a log line.
"""

from __future__ import annotations

import base64
import logging
import sqlite3

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import pytest

from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_admission import host_admission
from apps.admin_console.services.host_registry import host_registry

HOST = {"Host": "localhost"}
PHONE = "sd-05b42e59020251f2"
AVD = "sd-8bcfa99c1405b169"
RAW = ["R5CT1234ABC", "192.168.1.5:5555", "emulator-5554", "physical:R5CT1234ABC", "avd:Pixel_A"]


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", "enabled")
    monkeypatch.setattr(host_registry, "db_path", tmp_path / "hosts.db")
    monkeypatch.setattr(host_registry, "clock", lambda: 1_800_000_000.0)
    host_registry.reset_memory()
    host_admission.reset()
    yield tmp_path / "hosts.db"
    host_registry.reset_memory()
    host_admission.reset()


@pytest.fixture
def admin():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000), base_url="http://localhost")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _enroll(admin, name: str):
    key = Ed25519PrivateKey.generate()
    code = admin.post("/api/hosts/enrollment-codes").json()["code"]
    public = _b64(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    response = admin.post(
        "/api/agent/enroll",
        headers={"cf-connecting-ip": "198.51.100.7"},
        json={
            "code": code,
            "public_key": public,
            "signature": _b64(key.sign(hr.enroll_message(code, public))),
            "name": name,
            "protocol_version": hr.PROTOCOL_VERSION,
        },
    )
    assert response.status_code == 200, response.text
    return key, response.json()


def _connect(admin, key, host_id):
    challenge = admin.post("/api/agent/challenge", json={"host_id": host_id}).json()
    timestamp = int(host_registry.clock())
    message = hr.connect_message(
        challenge["audience"], hr.PROTOCOL_VERSION, host_id, challenge["nonce"], timestamp
    )
    context = admin.websocket_connect("/api/agent/connect", headers=HOST)
    ws = context.__enter__()
    ws.send_json(
        {
            "type": "hello",
            "host_id": host_id,
            "nonce": challenge["nonce"],
            "protocol_version": hr.PROTOCOL_VERSION,
            "timestamp": timestamp,
            "signature": _b64(key.sign(message)),
        }
    )
    assert ws.receive_json()["type"] == "connected"
    return context, ws


def _sync(ws) -> None:
    ws.send_json({"type": "ping"})
    assert ws.receive_json()["type"] == "pong"


def test_enrollment_delivers_one_org_pepper(admin):
    _, first = _enroll(admin, "Lab Mac")
    _, second = _enroll(admin, "Desk PC")
    assert len(base64.b64decode(first["device_pepper"], validate=True)) == 32
    assert first["device_pepper"] == second["device_pepper"]
    host_registry.reset_memory()
    assert host_registry.device_pepper() == first["device_pepper"]


def test_registry_accepts_only_opaque_device_ids(admin):
    key, enrolled = _enroll(admin, "Lab Mac")
    context, ws = _connect(admin, key, enrolled["host_id"])
    try:
        ws.send_json(
            {
                "type": "devices",
                "devices": [
                    {"serial": PHONE, "model": "Pixel 8", "kind": "physical", "shared": True},
                    {"serial": "R5CT1234ABC", "model": "Pixel 8", "shared": True},
                    {"serial": "SD-05B42E59020251F2", "shared": True},
                    {"serial": AVD, "kind": "emulator", "shared": False},
                ],
            }
        )
        _sync(ws)
    finally:
        context.__exit__(None, None, None)
    body = admin.get("/api/hosts").json()
    assert [d["serial"] for d in body["devices"]] == [PHONE]
    assert body["hosts"][0]["unshared_serials"] == [AVD]
    assert (body["hosts"][0]["phones_shared"], body["hosts"][0]["phones_not_shared"]) == (1, 1)


def test_same_physical_device_on_two_computers_is_flagged_on_both(admin):
    connections = []
    for name in ["Lab Mac", "Desk PC"]:
        key, enrolled = _enroll(admin, name)
        context, ws = _connect(admin, key, enrolled["host_id"])
        connections.append((context, enrolled["host_id"]))
        ws.send_json(
            {
                "type": "devices",
                "devices": [
                    {"serial": PHONE, "kind": "physical", "shared": True},
                    {"serial": AVD, "kind": "emulator", "shared": True},
                ],
            }
        )
        _sync(ws)
    try:
        body = admin.get("/api/hosts").json()
        names = {host["id"]: host["name"] for host in body["hosts"]}
        for host in body["hosts"]:
            other = next(name for hid, name in names.items() if hid != host["id"])
            assert host["attention"] == [
                {"serial": PHONE, "reason": "also_visible", "also_visible_on": [other]}
            ]
        flagged = [d for d in body["devices"] if d["serial"] == PHONE]
        assert len(flagged) == 2
        assert all(d["attention"] == "also_visible" for d in flagged)
        # Emulators share an AVD name across computers without being one device.
        assert all(d["attention"] is None for d in body["devices"] if d["serial"] == AVD)
        # B5a-1 admission sees both claims and dispatches to neither.
        assert sorted(host_admission.needs_attention(PHONE)) == sorted(names)
        assert host_admission.needs_attention(AVD) == []
    finally:
        for context, _ in connections:
            context.__exit__(None, None, None)


def test_agent_audit_events_are_logged_with_opaque_ids_only(admin, caplog):
    key, enrolled = _enroll(admin, "Lab Mac")
    context, ws = _connect(admin, key, enrolled["host_id"])
    caplog.set_level(logging.DEBUG)
    try:
        for event in [
            {"type": "event", "event": "share_mode_changed", "mode": "auto"},
            {"type": "event", "event": "device_auto_shared", "device": PHONE},
            {
                "type": "event",
                "event": "device_share_changed",
                "device": PHONE,
                "shared": False,
                "by": "cli",
            },
            {"type": "event", "event": "identity_ambiguous", "device": AVD},
            {"type": "event", "event": "device_auto_shared", "device": "R5CT1234ABC"},
            {"type": "event", "event": "custom", "device": PHONE},
            {"type": "event", "event": "share_mode_changed", "mode": "emulator-5554"},
        ]:
            ws.send_json(event)
        _sync(ws)
    finally:
        context.__exit__(None, None, None)
    # The app's logging filter may prefix a session id; audit fields start at "event=".
    lines = [record.getMessage() for record in caplog.records]
    audit = [line[line.index("event=") :] for line in lines if "event=" in line]
    host_id = enrolled["host_id"]
    assert f"event=share_mode_changed host_id={host_id} mode=auto" in audit
    assert f"event=device_auto_shared host_id={host_id} device={PHONE}" in audit
    assert (
        f"event=device_share_changed host_id={host_id} device={PHONE} shared=false by=cli" in audit
    )
    assert f"event=identity_ambiguous host_id={host_id} device={AVD}" in audit
    assert not [line for line in audit if "custom" in line]


def test_leak_scan_over_server_payloads_logs_and_index(admin, caplog, registry):
    caplog.set_level(logging.DEBUG)
    key, enrolled = _enroll(admin, "Lab Mac")
    context, ws = _connect(admin, key, enrolled["host_id"])
    try:
        ws.send_json(
            {
                "type": "devices",
                "devices": [
                    {"serial": PHONE, "model": "Pixel 8", "kind": "physical", "shared": True},
                    *({"serial": raw, "model": raw, "shared": True} for raw in RAW),
                ],
            }
        )
        for raw in RAW:
            ws.send_json({"type": "event", "event": "device_auto_shared", "device": raw})
            ws.send_json({"type": "event", "event": "share_mode_changed", "mode": raw})
        _sync(ws)
    finally:
        context.__exit__(None, None, None)
    payloads = [admin.get("/api/hosts").text, admin.get("/api/hosts").text]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    with sqlite3.connect(registry) as connection:
        index = "\n".join(connection.iterdump())
    for raw in RAW:
        for name, text in [("payload", "\n".join(payloads)), ("log", logs), ("index", index)]:
            assert raw not in text, f"{raw} leaked into the {name}"
    assert PHONE in payloads[0]
