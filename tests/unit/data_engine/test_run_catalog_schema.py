# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Run catalog storage: migration, backfill, triggers and rebuild (CHE-1091).

The catalog lives in side tables of the unified sessions database
(`run_meta`, `run_recording_state`, `runs_fts`). Sessions are rewritten by
upserts, so nothing the catalog owns may live in a `sessions` column.
"""

import json
from pathlib import Path
import sqlite3
import uuid

import pytest


@pytest.fixture
def rc():
    from artemis.data_engine import run_catalog

    return run_catalog


def _legacy_db(path: Path, rows: list[tuple]) -> None:
    """A database as it exists today, before the catalog: bare sessions table."""
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, initial_goal TEXT, "
            "start_time REAL, end_time REAL, status TEXT, device_info TEXT, video_filepath TEXT)"
        )
        conn.executemany(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _search(rc, db: Path, text: str) -> list[str]:
    with sqlite3.connect(db) as conn:
        return rc.search_session_ids(conn, text)


def test_migrate_backfills_legacy_rows_and_leaves_sessions_untouched(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    ids = [str(uuid.uuid4()) for _ in range(3)]
    _legacy_db(
        db,
        [
            (ids[0], "open settings and enable wifi", 1.0, "completed", '{"device_id": "emu-1"}'),
            (ids[1], "send a message", 2.0, "success", None),
            ("legacy-run_1.0", "legacy id run", 3.0, "failed", "not json"),
        ],
    )
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()

    report = rc.migrate(db)

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        after = [tuple(r) for r in conn.execute("SELECT * FROM sessions ORDER BY session_id")]
        assert after == before
        meta = {r["session_id"]: r for r in conn.execute("SELECT * FROM run_meta")}
    assert set(meta) == {ids[0], ids[1], "legacy-run_1.0"}
    assert json.loads(meta[ids[0]]["device_ref"]) == {"host_id": None, "serial": "emu-1"}
    assert meta[ids[1]]["device_ref"] is None  # no device_info, nothing invented
    assert meta[ids[0]]["deleted_at"] is None and meta[ids[0]]["pinned"] == 0
    assert report.backfilled == 3
    assert _search(rc, db, "wifi") == [ids[0]]


def test_migrate_takes_an_online_backup_that_includes_uncheckpointed_wal(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    _legacy_db(db, [])
    live = sqlite3.connect(db)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.executemany(
        "INSERT INTO sessions (session_id, initial_goal, start_time, status) VALUES (?,?,?,?)",
        [(str(uuid.uuid4()), f"goal {i}", float(i), "completed") for i in range(5)],
    )
    live.commit()
    assert Path(f"{db}-wal").stat().st_size > 0  # rows exist only in the WAL

    report = rc.migrate(db)
    live.close()

    assert report.backup_path is not None and report.backup_path.exists()
    with sqlite3.connect(report.backup_path) as backup:
        assert backup.execute("SELECT count(*) FROM sessions").fetchone()[0] == 5
        # the backup is the pre-migration state: no catalog tables yet
        names = {r[0] for r in backup.execute("SELECT name FROM sqlite_master")}
    assert "run_meta" not in names


def test_migrate_is_idempotent_and_backs_up_once(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    _legacy_db(db, [(str(uuid.uuid4()), "goal", 1.0, "completed", None)])

    first = rc.migrate(db)
    second = rc.migrate(db)

    assert first.backup_path is not None
    assert second.backup_path is None and second.backfilled == 0
    assert len(list(tmp_path.glob("data_engine.db.pre-run-catalog.*"))) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM run_meta").fetchone()[0] == 1


def test_migrate_of_an_empty_database_makes_no_backup(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    _legacy_db(db, [])
    assert rc.migrate(db).backup_path is None
    assert not list(tmp_path.glob("*.pre-run-catalog.*"))


def test_triggers_keep_the_index_in_step_with_sessions_and_run_meta(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    _legacy_db(db, [])
    rc.migrate(db)
    sid = str(uuid.uuid4())

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status) "
            "VALUES (?, 'rotate the screen', 1.0, 'running')",
            (sid,),
        )
    assert _search(rc, db, "rotate") == [sid]  # insert trigger made run_meta + index row

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE sessions SET initial_goal = 'mute the volume' WHERE session_id = ?", (sid,)
        )
    assert _search(rc, db, "rotate") == []
    assert _search(rc, db, "volume") == [sid]

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE run_meta SET requested_by = 'dana@example.com' WHERE session_id = ?", (sid,)
        )
    assert _search(rc, db, "dana") == [sid]  # run_meta text is searchable too

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        row = conn.execute(
            "SELECT deleted_at, deleted_reason FROM run_meta WHERE session_id = ?", (sid,)
        ).fetchone()
    assert _search(rc, db, "volume") == []
    assert row[0] is not None and row[1] == "session_deleted"  # hard delete leaves a tombstone


def test_rebuild_restores_a_wiped_index_and_drops_stale_rows(tmp_path, rc):
    db = tmp_path / "data_engine.db"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _legacy_db(
        db, [(a, "alpha task", 1.0, "completed", None), (b, "beta task", 2.0, "completed", None)]
    )
    rc.migrate(db)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM runs_fts")
        conn.execute("INSERT INTO runs_fts(rowid, prompt, meta) VALUES (9999, 'ghost', '')")
    assert _search(rc, db, "alpha") == []

    with sqlite3.connect(db) as conn:
        count = rc.rebuild(conn)

    assert count == 2
    assert _search(rc, db, "alpha") == [a]
    assert _search(rc, db, "beta") == [b]
    assert _search(rc, db, "ghost") == []


def test_session_upsert_does_not_reset_run_meta(tmp_path, rc):
    """The hazard behind side tables: StorageManager upserts rewrite session rows."""
    from artemis.data_engine.storage import StorageManager
    from artemis.data_engine.models import SessionMetadata

    db = tmp_path / "data_engine.db"
    manager = StorageManager(db, tmp_path)
    sid = uuid.uuid4()
    meta = SessionMetadata(
        session_id=sid, initial_goal="first goal", start_time=1.0, status="running", device_info={}
    )
    manager.create_session(meta)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE run_meta SET pinned = 1, requested_by = 'ops' WHERE session_id = ?", (str(sid),)
        )

    meta.initial_goal = "second goal"
    manager.create_session(meta)  # ON CONFLICT upsert of the same session id

    with sqlite3.connect(db) as conn:
        pinned, requester = conn.execute(
            "SELECT pinned, requested_by FROM run_meta WHERE session_id = ?", (str(sid),)
        ).fetchone()
    assert (pinned, requester) == (1, "ops")
    assert _search(rc, db, "second") == [str(sid)]
    assert _search(rc, db, "first") == []


def test_storage_manager_installs_the_catalog_on_a_fresh_database(tmp_path, rc):
    from artemis.data_engine.storage import StorageManager

    db = tmp_path / "data_engine.db"
    StorageManager(db, tmp_path)
    with sqlite3.connect(db) as conn:
        assert rc.catalog_ready(conn)
        assert rc.search_mode(conn) == "fts"


def test_missing_fts5_degrades_to_substring_search(tmp_path, rc, monkeypatch):
    db = tmp_path / "data_engine.db"
    sid = str(uuid.uuid4())
    _legacy_db(db, [(sid, "check battery saver", 1.0, "completed", None)])
    monkeypatch.setattr(rc, "fts5_available", lambda conn: False)

    rc.migrate(db)

    with sqlite3.connect(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert "runs_fts" not in names and "run_meta" in names
        assert rc.search_mode(conn) == "substring"
    assert _search(rc, db, "battery") == [sid]  # substring fallback still finds it


@pytest.mark.parametrize(
    "raw",
    [
        "foo OR bar",
        "NEAR(a b)",
        '"unbalanced',
        "col:foo",
        "foo*",
        "-bar",
        "(a",
        "a'b",
        "^x",
        "a AND NOT b",
    ],
)
def test_search_text_is_quoted_so_raw_fts_syntax_never_reaches_match(tmp_path, rc, raw):
    match = rc.build_match_query(raw)
    # every term is a double-quoted literal joined by implicit AND: no operators survive
    assert match is not None
    assert all(part.startswith('"') and part.rstrip("*").endswith('"') for part in match.split(" "))
    db = tmp_path / "data_engine.db"
    _legacy_db(db, [(str(uuid.uuid4()), "foo bar baz", 1.0, "completed", None)])
    rc.migrate(db)
    _search(rc, db, raw)  # must not raise sqlite3.OperationalError


def test_search_text_without_terms_matches_nothing(tmp_path, rc):
    assert rc.build_match_query("") is None
    assert rc.build_match_query('!!! "" ---') is None


def test_catalog_cli_migrates_backfills_and_rebuilds(tmp_path):
    from typer.testing import CliRunner

    from artemis.interfaces.cli.main import app

    db = tmp_path / "data_engine.db"
    sid = str(uuid.uuid4())
    _legacy_db(db, [(sid, "toggle dark mode", 1.0, "completed", None)])
    runner = CliRunner()

    migrated = runner.invoke(app, ["catalog", "migrate", "--db", str(db)])
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM runs_fts")
    rebuilt = runner.invoke(app, ["catalog", "rebuild", "--db", str(db)])
    backfilled = runner.invoke(app, ["catalog", "backfill", "--db", str(db)])

    assert migrated.exit_code == 0 and "1 runs backfilled" in migrated.output
    assert rebuilt.exit_code == 0 and "1 runs indexed" in rebuilt.output
    assert backfilled.exit_code == 0 and "0 runs backfilled" in backfilled.output
    from artemis.data_engine import run_catalog

    assert _search(run_catalog, db, "dark") == [sid]


# -- review round 1 (Sol): R3, R5 -----------------------------------------------


def _fallback_database(tmp_path, monkeypatch, rc) -> Path:
    """A catalog created while FTS5 was unavailable, then FTS5 comes back."""
    from artemis.data_engine.storage import StorageManager

    db = tmp_path / "data_engine.db"
    available = rc.fts5_available
    monkeypatch.setattr(rc, "fts5_available", lambda conn: False)
    StorageManager(db, tmp_path)
    monkeypatch.setattr(rc, "_test_restore_fts", available, raising=False)
    return db


def _insert(db: Path, goal: str, sid: str | None = None, start: float = 1.0, replace=False) -> str:
    sid = sid or str(uuid.uuid4())
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"{verb} INTO sessions (session_id, initial_goal, start_time, status) "
            "VALUES (?, ?, ?, 'completed')",
            (sid, goal, start),
        )
    return sid


def test_promotion_to_fts_indexes_runs_created_in_substring_mode(tmp_path, rc, monkeypatch):
    db = _fallback_database(tmp_path, monkeypatch, rc)
    existing = _insert(db, "review wifi")
    monkeypatch.setattr(rc, "fts5_available", rc._test_restore_fts)

    report = rc.migrate(db)
    created_later = _insert(db, "review wifi again", start=2.0)

    assert report.search_mode == "fts"
    assert set(_search(rc, db, "wifi")) == {existing, created_later}


def test_promotion_to_fts_replaces_the_plain_triggers(tmp_path, rc, monkeypatch):
    db = _fallback_database(tmp_path, monkeypatch, rc)
    sid = _insert(db, "review wifi")
    monkeypatch.setattr(rc, "fts5_available", rc._test_restore_fts)
    rc.migrate(db)
    with sqlite3.connect(db) as conn:
        rc.rebuild(conn)

    _insert(db, "replacement bluetooth", sid=sid, start=2.0, replace=True)

    assert _search(rc, db, "bluetooth") == [sid]
    assert _search(rc, db, "wifi") == []  # no stale text from before the rewrite


@pytest.mark.parametrize("command", ["migrate", "backfill", "rebuild"])
def test_catalog_cli_rejects_a_missing_database_without_creating_it(tmp_path, command):
    from typer.testing import CliRunner

    from artemis.interfaces.cli.commands.catalog import catalog_app

    db = tmp_path / "misspelled.db"
    result = CliRunner().invoke(catalog_app, [command, "--db", str(db)])

    assert result.exit_code != 0, result.output
    assert not db.exists()


def test_catalog_cli_migrate_refuses_a_database_without_sessions(tmp_path):
    from typer.testing import CliRunner

    from artemis.interfaces.cli.commands.catalog import catalog_app

    db = tmp_path / "other.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
    result = CliRunner().invoke(catalog_app, ["migrate", "--db", str(db)])
    assert result.exit_code != 0 and "Catalog ready" not in result.output


# -- review round 2 (Sol): R6 -----------------------------------------------------


def _substring_mode_database(tmp_path, rc) -> Path:
    from unittest.mock import patch

    from artemis.data_engine.storage import StorageManager

    db = tmp_path / "data_engine.db"
    with patch.object(rc, "fts5_available", return_value=False):
        StorageManager(db, tmp_path)
    return db


def test_failed_promotion_leaves_nothing_behind_and_a_retry_indexes_old_runs(tmp_path, rc):
    from unittest.mock import patch

    db = _substring_mode_database(tmp_path, rc)
    sid = _insert(db, "review wifi")

    with patch.object(rc, "rebuild", side_effect=sqlite3.OperationalError("injected")):
        with pytest.raises(sqlite3.OperationalError):
            rc.migrate(db)
    with sqlite3.connect(db) as conn:
        assert rc.search_mode(conn) == "substring"  # nothing half-published

    report = rc.migrate(db)

    assert report.search_mode == "fts"
    assert _search(rc, db, "wifi") == [sid]  # the pre-existing run is in the index


def test_promotion_is_one_transaction_that_excludes_a_concurrent_delete(tmp_path, rc):
    db = _substring_mode_database(tmp_path, rc)
    sid = _insert(db, "review wifi")
    seen: dict = {}

    class Interleaving(sqlite3.Connection):
        def execute(self, statement, parameters=()):
            result = super().execute(statement, parameters)
            if statement.startswith("DROP TRIGGER") and "writer" not in seen:
                seen["in_transaction"] = self.in_transaction
                try:  # a second connection tries to delete inside the drop/recreate gap
                    with sqlite3.connect(db, timeout=0.2) as writer:
                        writer.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                    seen["writer"] = "committed"
                except sqlite3.OperationalError:
                    seen["writer"] = "blocked"
            return result

    with sqlite3.connect(db, factory=Interleaving) as conn:
        rc.ensure_schema(conn)

    assert seen["in_transaction"] is True
    assert seen["writer"] == "blocked"  # serialized behind the promotion
    with sqlite3.connect(db) as conn:
        # either way the invariant holds: a missing session always has a tombstone
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        assert (
            conn.execute("SELECT deleted_at FROM run_meta WHERE session_id = ?", (sid,)).fetchone()[
                0
            ]
            is not None
        )
