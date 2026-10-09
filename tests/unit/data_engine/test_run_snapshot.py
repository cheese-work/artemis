"""Run snapshot columns: revision, backup and resumable backfill (CHE-1469; docs/board-operations.md)."""

import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from artemis.data_engine import run_catalog, run_snapshot, schema_revisions

FIELDS = ("app_build", "suite_version", "device_model", "agent_model")


def _old_db(path: Path, infos: list[dict | None], host_model: str | None = None) -> list[str]:
    """A database as the previous binary leaves it: catalog installed, no snapshot columns."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, "
            "start_time REAL, end_time REAL, status TEXT, device_info TEXT, video_filepath TEXT)"
        )
        conn.execute(
            "CREATE TABLE host_devices (host_id TEXT NOT NULL, serial TEXT NOT NULL, model TEXT, "
            "shared INTEGER NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (host_id, serial))"
        )
        if host_model:
            conn.execute(
                "INSERT INTO host_devices VALUES ('host-a', 'emu-host', ?, 0, 1.0)", (host_model,)
            )
    run_catalog.migrate(path)
    ids = []
    with sqlite3.connect(path) as conn:
        for i, info in enumerate(infos):
            sid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info) "
                "VALUES (?, ?, ?, 'completed', ?)",
                (sid, f"run {i}", float(i), json.dumps(info) if info is not None else None),
            )
            ids.append(sid)
    return ids


def _snapshots(db: Path) -> dict[str, tuple]:
    with sqlite3.connect(db) as conn:
        return {
            row[0]: tuple(row[1:])
            for row in conn.execute(
                f"SELECT session_id, {', '.join(FIELDS)} FROM run_meta ORDER BY rid"
            )
        }


def _progress(db: Path) -> tuple:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT cursor, done, total FROM backfill_progress WHERE module = 'run_meta_ext'"
        ).fetchone()


def test_upgrade_adds_nullable_columns_after_a_backup_and_records_the_revision(tmp_path):
    db = tmp_path / "data_engine.db"
    ids = _old_db(
        db,
        [
            {"device_id": "emu-1", "app_build": "4.2.0 (420)", "device_model": "Pixel 6 Pro"},
            {"device_id": "emu-host"},
            None,
        ],
        host_model="Pixel 3a",
    )
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE run_meta SET host_id = 'host-a' WHERE session_id = ?", (ids[1],))

    report = run_snapshot.migrate(db)

    assert report.backup_path is not None and report.backup_path.exists()
    with sqlite3.connect(report.backup_path) as backup:
        columns = {row[1] for row in backup.execute("PRAGMA table_info(run_meta)")}
        assert not columns & set(FIELDS)  # the backup is the pre-revision state
    with sqlite3.connect(db) as conn:
        assert schema_revisions.current(conn, "run_meta_ext") == 1
    snaps = _snapshots(db)
    assert snaps[ids[0]] == ("4.2.0 (420)", None, "Pixel 6 Pro", None)
    assert snaps[ids[1]] == (None, None, "Pixel 3a", None)  # from the device record
    assert snaps[ids[2]] == (None, None, None, None)  # unknown stays unknown
    assert _progress(db) == (str(_rid(db, ids[2])), 3, 3)

    again = run_snapshot.migrate(db)  # safe to repeat: no second backup, nothing pending
    assert again.backup_path is None and again.backfilled == 0


def _rid(db: Path, sid: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT rid FROM run_meta WHERE session_id = ?", (sid,)).fetchone()[0]


def test_backfill_interrupted_mid_run_resumes_without_duplicates(tmp_path):
    db = tmp_path / "data_engine.db"
    ids = _old_db(db, [{"device_id": f"emu-{i}", "app_build": f"b{i}"} for i in range(7)])
    with sqlite3.connect(db) as conn:
        schema_revisions.apply(conn, "run_meta_ext", run_snapshot.REVISIONS)
        # A crash in the third batch: the first two batches are committed, the third is not.
        conn.execute(
            "CREATE TEMP TRIGGER crash BEFORE UPDATE ON run_meta "
            f"WHEN old.rid = {_rid(db, ids[5])} BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="crash"):
            run_snapshot.backfill(conn, batch=2)
    assert _progress(db) == (str(_rid(db, ids[3])), 4, 7)
    assert [s[0] for s in _snapshots(db).values()] == ["b0", "b1", "b2", "b3", None, None, None]

    with sqlite3.connect(db) as conn:  # restart: the temp trigger is gone
        resumed = run_snapshot.backfill(conn, batch=2)

    assert resumed == 3  # only the rows after the cursor
    assert _progress(db) == (str(_rid(db, ids[6])), 7, 7)
    assert [s[0] for s in _snapshots(db).values()] == [f"b{i}" for i in range(7)]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM run_meta").fetchone()[0] == 7
        assert run_snapshot.backfill(conn, batch=2) == 0  # finished stays finished


def test_execution_snapshot_is_written_once_and_kept_across_a_worker_restart(tmp_path):
    from artemis.data_engine.models import SessionMetadata
    from artemis.data_engine.storage import StorageManager

    storage = StorageManager(tmp_path / "data_engine.db", tmp_path)
    sid = uuid.uuid4()
    info = {"device_id": "emu-1", "device_model": "Pixel 6 Pro", "agent_model": "gemini-3.8-flash"}
    storage.create_session(SessionMetadata(session_id=sid, initial_goal="g", device_info=info))
    storage.create_session(  # same session id again, now with other values
        SessionMetadata(
            session_id=sid,
            initial_goal="g",
            device_info={**info, "device_model": "other", "app_build": "late"},
        )
    )

    assert _snapshots(tmp_path / "data_engine.db")[str(sid)] == (
        "late",  # first known value fills an unknown one
        None,
        "Pixel 6 Pro",  # a recorded value is never rewritten
        "gemini-3.8-flash",
    )


def test_execution_fields_read_the_planner_model_and_the_device_model():
    class Device:
        prop = type("Prop", (), {"model": "Pixel 6 Pro"})()

    class Adb:
        def device(self, serial):
            assert serial == "emu-1"
            return Device()

    class Broken:
        def device(self, serial):
            raise RuntimeError("adb offline")

    planner = type("Planner", (), {"model": "gemini-3.8-flash"})()
    llm = type("Llm", (), {"planner": planner})()

    assert run_snapshot.execution_fields(Adb(), "emu-1", llm) == {
        "device_model": "Pixel 6 Pro",
        "agent_model": "gemini-3.8-flash",
    }
    assert run_snapshot.execution_fields(Broken(), "emu-1", None) == {}  # unknown, not guessed
