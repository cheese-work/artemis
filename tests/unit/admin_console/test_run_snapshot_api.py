"""Run snapshot at the server API seam, and the previous binary on the new schema (CHE-1469)."""

import sqlite3
import time
import uuid

import pytest

from artemis.data_engine import run_catalog, run_snapshot
from artemis.data_engine.models import SessionMetadata
from artemis.data_engine.storage import StorageManager

FIELDS = ("app_build", "suite_version", "device_model", "agent_model")
# The previous binary's catalog read (run_catalog_repository._COLUMNS before CHE-1469).
OLD_COLUMNS = (
    "m.session_id, s.initial_goal, s.start_time, s.end_time, s.status, s.interrupt_reason, "
    "m.host_id, m.device_ref, m.requested_by, m.pinned, m.deleted_at, m.deleted_reason"
)


@pytest.mark.asyncio
async def test_a_new_run_stores_its_snapshot_and_an_old_run_reads_as_unknown(library, admin):
    old = library.seed("before the upgrade")  # the old binary's insert: no snapshot
    new = str(uuid.uuid4())
    StorageManager(library.db, library.traces).create_session(
        SessionMetadata(
            session_id=new,
            initial_goal="after the upgrade",
            start_time=time.time(),
            device_info={
                "device_id": "emu-1",
                "device_model": "Pixel 6 Pro",
                "agent_model": "gemini-3.8-flash",
            },
        )
    )

    async with admin:
        fresh = (await admin.get(f"/api/runs/{new}")).json()
        legacy = (await admin.get(f"/api/runs/{old}")).json()
        listed = {
            r["session_id"]: r
            for r in (await admin.get("/api/runs", params={"scope": "all"})).json()["runs"]
        }

    assert {k: fresh[k] for k in FIELDS} == {
        "app_build": None,  # no build source exists yet: unknown, not guessed
        "suite_version": None,  # CHE-1339 hook
        "device_model": "Pixel 6 Pro",
        "agent_model": "gemini-3.8-flash",
    }
    assert {k: legacy[k] for k in FIELDS} == dict.fromkeys(FIELDS)
    assert {k: listed[new][k] for k in FIELDS} == {k: fresh[k] for k in FIELDS}


@pytest.mark.asyncio
async def test_the_previous_binary_works_on_the_new_schema_including_its_retention_sweep(
    library, admin
):
    expired = library.seed("expired", age_days=40)
    kept = library.seed("kept", age_days=1)
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "UPDATE run_meta SET device_model = 'Pixel 6 Pro', agent_model = 'm' "
            "WHERE session_id IN (?, ?)",
            (expired, kept),
        )
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        # Its presence checks see a complete catalog and change nothing.
        assert run_catalog.ensure_schema(conn) is True
        assert (
            conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
            == schema
        )
        # Its reads and its inserts ignore the nullable columns.
        rows = conn.execute(
            f"SELECT {OLD_COLUMNS} FROM sessions s JOIN run_meta m ON m.session_id = s.session_id"
        ).fetchall()
        assert {row[0] for row in rows} == {expired, kept}
    run_catalog.migrate(library.db)  # the old startup path

    async with admin:
        assert (await admin.put("/api/system/retention", json={"days": 30})).status_code == 200
        assert (await admin.post("/api/system/retention/dry-run")).status_code == 200
        assert (await admin.put("/api/system/retention", json={"enabled": True})).status_code == 200
        result = (await admin.post("/api/system/retention/run")).json()
        assert result["deleted"] == [expired]
        assert (await admin.get(f"/api/runs/{expired}")).status_code == 410
        assert (await admin.get(f"/api/runs/{kept}")).json()["device_model"] == "Pixel 6 Pro"

    # The next upgrade finds nothing pending and keeps the revision.
    report = run_snapshot.migrate(library.db)
    assert report.backup_path is None and report.backfilled == 0
    with sqlite3.connect(library.db) as conn:
        assert conn.execute(
            "SELECT revision FROM schema_revisions WHERE module = 'run_meta_ext'"
        ).fetchone() == (len(run_snapshot.REVISIONS),)
