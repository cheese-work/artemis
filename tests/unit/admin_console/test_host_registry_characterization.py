"""Characterization tests for the computer registry (CHE-1156, audit of CHE-1095 / #58).

Written after the merged slice to pin two behaviours the original PR did not
test first: single-winner enrollment under contention, and additive
initialisation over a pre-existing Artemis database. Each carries a negative
control in the PR description.
"""

from __future__ import annotations

import base64
from contextlib import closing
import sqlite3
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from apps.admin_console.database import connection as db_connection
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_registry import HostRegistry, RegistryError

CONTENDERS = 4
# Enough to let every contender read the code row before any of them writes.
READ_TO_WRITE_GAP_SECONDS = 0.3


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _pub(key: Ed25519PrivateKey) -> str:
    return _b64(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _body(code: str, key: Ed25519PrivateKey) -> dict:
    public = _pub(key)
    return {
        "code": code,
        "public_key": public,
        "signature": _b64(key.sign(hr.enroll_message(code, public))),
        "name": "Lab Mac",
        "os": "macOS 15",
        "agent_version": "0.1.0",
        "protocol_version": hr.PROTOCOL_VERSION,
    }


class _SlowCodeRead:
    """Connection proxy: lingers after the code lookup, so a missing write lock is visible."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if sql.lstrip().startswith("SELECT * FROM host_enrollment_codes"):
            time.sleep(READ_TO_WRITE_GAP_SECONDS)
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = HostRegistry()
    reg.db_path = tmp_path / "hosts.db"
    real_get_db = db_connection.get_db
    monkeypatch.setattr(hr, "get_db", lambda path=None: _SlowCodeRead(real_get_db(path)))
    return reg


def _race(registry: HostRegistry, bodies: list[dict]) -> list[dict | RegistryError]:
    barrier = threading.Barrier(len(bodies))
    results: list[dict | RegistryError] = [None] * len(bodies)  # type: ignore[list-item]

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = registry.enroll(bodies[index])
        except RegistryError as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(bodies))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert all(result is not None for result in results), "an enroll call never returned"
    return results


def _rows(registry: HostRegistry, sql: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(registry.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchall()


def test_concurrent_first_keys_bind_exactly_one_host(registry):
    code = registry.create_code("admin@example.com")["code"]
    keys = [Ed25519PrivateKey.generate() for _ in range(CONTENDERS)]

    results = _race(registry, [_body(code, key) for key in keys])

    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, RegistryError)]
    assert len(winners) == 1
    assert [e.code for e in losers] == ["code_used"] * (CONTENDERS - 1)
    assert [e.status for e in losers] == [409] * (CONTENDERS - 1)

    hosts = _rows(registry, "SELECT id FROM hosts")
    assert [h["id"] for h in hosts] == [winners[0]["host_id"]]  # no orphan host rows
    bound = _rows(registry, "SELECT host_id, key_hash FROM host_enrollment_codes")
    assert [b["host_id"] for b in bound] == [winners[0]["host_id"]]
    assert _rows(registry, "SELECT key_hash FROM hosts")[0]["key_hash"] == bound[0]["key_hash"]


def test_simultaneous_same_key_retries_stay_idempotent(registry):
    code = registry.create_code("admin@example.com")["code"]
    key = Ed25519PrivateKey.generate()

    results = _race(registry, [_body(code, key)] * CONTENDERS)

    assert all(isinstance(r, dict) for r in results), results
    assert len({r["host_id"] for r in results}) == 1
    assert len(_rows(registry, "SELECT id FROM hosts")) == 1
    assert (
        len(_rows(registry, "SELECT id FROM host_enrollment_codes WHERE host_id IS NOT NULL")) == 1
    )


def test_additive_init_keeps_pre_b1_database_and_still_enrolls(tmp_path):
    from artemis.data_engine.storage import StorageManager

    db_path = tmp_path / "artemis.db"
    StorageManager(db_path=db_path, base_trace_dir=tmp_path)  # the pre-B1 (main) schema
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status) "
            "VALUES ('sentinel-session', 'keep me', 1.0, 'completed')"
        )
        conn.execute(
            "INSERT INTO lifecycle_events (event_id, event_type, session_id, payload, recorded_at) "
            "VALUES ('sentinel-event', 'finished', 'sentinel-session', '{}', 1.0)"
        )
        conn.execute(
            "INSERT INTO lifecycle_outbox (dedupe_id, session_id, status, created_at) "
            "VALUES ('sentinel-outbox', 'sentinel-session', 'completed', 1.0)"
        )
    with closing(sqlite3.connect(db_path)) as conn:
        before = {
            name: conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608 - fixed names
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "hosts" not in before

    reg = HostRegistry()
    reg.db_path = db_path
    key = Ed25519PrivateKey.generate()
    host_id = reg.enroll(_body(reg.create_code("admin@example.com")["code"], key))["host_id"]
    nonce = reg.issue_nonce(host_id, "aud")
    timestamp = int(reg.clock())
    hello = {
        "type": "hello",
        "host_id": host_id,
        "nonce": nonce,
        "protocol_version": hr.PROTOCOL_VERSION,
        "timestamp": timestamp,
        "signature": _b64(
            key.sign(hr.connect_message("aud", hr.PROTOCOL_VERSION, host_id, nonce, timestamp))
        ),
        "agent_version": "0.1.0",
    }
    connection = reg.begin_connection(reg.authenticate_hello(hello, "aud"))
    assert connection["generation"] == 1

    with closing(sqlite3.connect(db_path)) as conn:
        for name, old_rows in before.items():
            assert conn.execute(f"SELECT * FROM {name}").fetchall() == old_rows, name  # noqa: S608
        tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(before) | {"hosts", "host_enrollment_codes", "host_devices", "host_tokens"} <= tables
