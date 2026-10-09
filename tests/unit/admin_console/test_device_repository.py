"""Stage 1 of CHE-1363: device tables, revisioned migration and repository (CHE-1472).

Contracts: docs/device-identity.md, docs/board-operations.md (migration contract).
"""

import sqlite3
import uuid

import pytest

from apps.admin_console.database.repositories.device_repository import (
    ConnectionKeyTaken,
    DeviceRepository,
    DeviceStoreNotReady,
)
from apps.admin_console.database.repositories.principal_repository import PrincipalRepository
from artemis.data_engine import devices, storage
from artemis.data_engine.models import SessionMetadata
from artemis.data_engine.storage import StorageManager

ISSUER = "https://team.cloudflareaccess.com"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data_engine.db"
    StorageManager(path, tmp_path)
    return path


@pytest.fixture
def repo(db):
    return DeviceRepository(db)


def _schema(path) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()


def _revision(path) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT revision FROM schema_revisions WHERE module = 'devices'"
        ).fetchone()[0]


# -- migration ----------------------------------------------------------------


def test_migration_runs_twice_with_the_same_result(db):
    first = _schema(db)
    assert _revision(db) == len(devices.REVISIONS)

    report = devices.migrate(db)

    assert report.applied == () and report.backup_path is None
    assert _schema(db) == first
    assert _revision(db) == len(devices.REVISIONS)
    with sqlite3.connect(db) as conn:
        index = conn.execute("PRAGMA index_info(idx_device_events_device_ts)").fetchall()
    assert [row[2] for row in index] == ["device_id", "ts"]


def test_migration_backs_up_an_existing_database_including_wal_first(tmp_path):
    db = tmp_path / "data_engine.db"
    live = sqlite3.connect(db)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT)")
    live.execute("INSERT INTO sessions VALUES ('s1', 'keep me')")
    live.commit()  # the row exists only in the WAL while `live` stays open

    report = devices.migrate(db)
    live.close()

    assert report.applied == tuple(range(1, len(devices.REVISIONS) + 1))
    assert report.backup_path is not None
    with sqlite3.connect(report.backup_path) as backup:
        assert backup.execute("SELECT initial_goal FROM sessions").fetchall() == [("keep me",)]
        names = {r[0] for r in backup.execute("SELECT name FROM sqlite_master")}
    assert "devices" not in names  # the backup is the pre-migration state
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT initial_goal FROM sessions").fetchall() == [("keep me",)]
    assert devices.migrate(db).backup_path is None  # nothing pending, no second backup


def test_a_revision_newer_than_this_binary_is_left_alone(db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE schema_revisions SET revision = 99 WHERE module = 'devices'")
    before = _schema(db)

    assert devices.migrate(db).applied == ()
    assert _schema(db) == before and _revision(db) == 99


def test_an_old_binary_still_works_on_the_new_schema(db, tmp_path, repo, monkeypatch):
    device = repo.create_device(owner_principal_id=None, label="Pixel 6 Pro")
    # The previous binary has no device migration: startup, writes and the
    # retention sweep (whole-run delete) must ignore the new tables.
    monkeypatch.setattr(storage, "migrate_devices", lambda db_path: None)
    old = StorageManager(db, tmp_path)
    sid = uuid.uuid4()
    old.create_session(SessionMetadata(session_id=sid, initial_goal="open settings"))
    assert old.get_session(sid) is not None
    old.delete_session(sid, delete_files=False)

    assert old.get_session(sid) is None
    assert repo.get(device.device_id) == device
    assert _revision(db) == len(devices.REVISIONS)


# -- repository ---------------------------------------------------------------


def test_create_get_and_list_devices(db, repo):
    principals = PrincipalRepository(db)
    alice = principals.ensure_user(ISSUER, "alice", "alice@example.test").id
    bob = principals.ensure_user(ISSUER, "bob", "bob@example.test").id
    mine = repo.create_device(owner_principal_id=alice, label="Pixel 6 Pro")
    theirs = repo.create_device(
        owner_principal_id=bob, label="Pixel 3a", hardware_hash="h-3a", match_state="confirmed"
    )

    assert repo.get(mine.device_id) == mine
    assert mine.match_state == "provisional" and mine.hardware_hash is None
    assert repo.list() == [mine, theirs]
    assert repo.list(owner_principal_id=bob) == [theirs]
    assert repo.get("dev_missing") is None


def test_ids_are_opaque_and_never_derived_from_a_serial(repo):
    device = repo.create_device(owner_principal_id=None, label="emulator-5554")
    connection_id = repo.upsert_connection(
        device.device_id, kind="avd", source="local", host_id=None, serial="emulator-5554"
    )

    assert device.device_id.startswith("dev_") and connection_id.startswith("con_")
    assert "5554" not in device.device_id and "5554" not in connection_id
    other = repo.create_device(owner_principal_id=None, label="emulator-5554")
    assert other.device_id != device.device_id


def test_alias_resolves_to_the_canonical_device(repo):
    survivor = repo.create_device(owner_principal_id="p-alice", label="Pixel 6 Pro")
    merged = repo.create_device(owner_principal_id="p-alice", label="Pixel 6 Pro (Wi-Fi)")
    older = repo.create_device(owner_principal_id="p-alice", label="Pixel 6 Pro (old agent)")

    repo.add_alias(older.device_id, merged.device_id)
    repo.add_alias(merged.device_id, survivor.device_id)

    assert repo.resolve(merged.device_id) == survivor.device_id
    assert repo.resolve(older.device_id) == survivor.device_id  # chains collapse to one hop
    assert repo.resolve(survivor.device_id) == survivor.device_id
    assert repo.get(older.device_id) == survivor
    assert repo.resolve("dev_unknown") is None


def test_a_connection_key_maps_to_one_device(repo):
    phone = repo.create_device(owner_principal_id="p-alice", label="Pixel 6 Pro")
    other = repo.create_device(owner_principal_id="p-bob", label="Pixel 3a")

    usb = repo.upsert_connection(
        phone.device_id, kind="usb", source="host", host_id="host_1", serial="ABC123"
    )
    again = repo.upsert_connection(
        phone.device_id, kind="usb", source="host", host_id="host_1", serial="ABC123"
    )
    local = repo.upsert_connection(
        phone.device_id, kind="wifi", source="local", host_id=None, serial="10.0.0.5:5555"
    )
    local_again = repo.upsert_connection(
        phone.device_id, kind="wifi", source="local", host_id=None, serial="10.0.0.5:5555"
    )

    assert again == usb and local_again == local  # NULL host_id still dedupes
    with pytest.raises(ConnectionKeyTaken):
        repo.upsert_connection(
            other.device_id, kind="usb", source="host", host_id="host_1", serial="ABC123"
        )
    assert repo.device_for_connection("host", "host_1", "ABC123") == phone.device_id
    assert repo.device_for_connection("local", None, "10.0.0.5:5555") == phone.device_id
    assert repo.device_for_connection("host", "host_2", "ABC123") is None


def test_repository_fails_closed_without_the_schema(db, repo):
    with sqlite3.connect(db) as conn:  # a failed or partial bootstrap
        conn.execute("DROP TABLE device_aliases")

    with pytest.raises(DeviceStoreNotReady):
        repo.get("dev_x")
