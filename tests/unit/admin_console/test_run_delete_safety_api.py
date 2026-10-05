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

"""Round 4: session-folder ownership, failed deletes retried, per-run isolation, lease safety."""

import asyncio
import errno
import os

import pytest

from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import run_leases, run_media, run_purge, run_retention


@pytest.mark.parametrize("sibling_state", ["pinned", "live"])
@pytest.mark.asyncio
async def test_a_sibling_whose_task_name_is_the_doomed_run_id_keeps_its_video(
    library, admin, sibling_state
):
    doomed = library.seed("doomed", age_days=3)
    library.write(doomed, "stdout.log", "own log")
    sibling = library.seed(
        "sibling",
        age_days=2,
        pinned=sibling_state == "pinned",
        status="running" if sibling_state == "live" else "completed",
    )
    kept = library.video_in(sibling, doomed, "recording.mkv", b"KEEP")  # folder == doomed id

    async with admin:
        assert (await admin.post(f"/api/runs/{doomed}/delete")).status_code == 200

    assert kept.read_bytes() == b"KEEP" and library.count("video_recordings", sibling) == 1
    assert not (library.traces / doomed / "stdout.log").exists()  # its own files still go
    assert library.count("sessions", doomed) == 0


@pytest.mark.asyncio
async def test_the_session_folder_goes_once_no_other_run_uses_it(library, admin):
    doomed = library.seed("doomed", age_days=3)
    sibling = library.seed("sibling", age_days=2)
    library.video_in(sibling, doomed, "recording.mkv")

    async with admin:
        await admin.post(f"/api/runs/{doomed}/delete")
        assert (library.traces / doomed).exists()
        await admin.post(f"/api/runs/{sibling}/delete")

    assert not (library.traces / doomed).exists()


@pytest.mark.asyncio
async def test_a_failed_delete_is_retried_and_keeps_the_rows_that_name_the_files(
    library, admin, monkeypatch
):
    sid = library.seed("stubborn", age_days=3)
    video = library.video(sid)
    real_unlink = os.unlink

    def failing(path, *args, **kwargs):
        if str(path).endswith("recording.mp4"):
            raise OSError(errno.EIO, "disk error")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing)
    async with admin:
        response = await admin.post(f"/api/runs/{sid}/delete")
    assert response.status_code == 200 and response.json()["cleanup"] == "deferred"
    assert video.exists() and library.count("video_recordings", sid) == 1
    assert run_leases.pending_count(run_catalog_repo.db_path) == 1

    monkeypatch.setattr(os, "unlink", real_unlink)
    run_retention.finish_pending_cleanups()
    assert not video.exists() and library.count("video_recordings", sid) == 0
    assert library.count("sessions", sid) == 0
    assert run_leases.pending_count(run_catalog_repo.db_path) == 0


@pytest.mark.asyncio
async def test_one_run_that_cannot_be_purged_does_not_stop_the_rest(library, admin, monkeypatch):
    bad = library.seed("bad", age_days=60)
    good = library.seed("good", age_days=50)
    real = run_purge.purge_run

    def purge(db_path, traces, session_id, **kwargs):
        if session_id == bad:
            raise RuntimeError("poisoned")
        return real(db_path, traces, session_id, **kwargs)

    monkeypatch.setattr(run_purge, "purge_run", purge)
    async with admin:
        assert (await admin.put("/api/system/retention", json={"days": 30})).status_code == 200
        await admin.post("/api/system/retention/dry-run")
        await admin.put("/api/system/retention", json={"enabled": True})
        result = (await admin.post("/api/system/retention/run")).json()

    assert library.count("sessions", good) == 0
    assert result["deleted"] == [good] and result["deferred"] == [bad]
    assert run_retention.get_settings()["last_enforced_at"] is not None


def test_a_poisoned_pending_cleanup_does_not_block_the_others(library, monkeypatch):
    bad, good = library.seed("bad"), library.seed("good")
    db = run_catalog_repo.db_path
    for sid in (bad, good):
        run_catalog_repo.tombstone(sid, "admin_delete")
        run_leases.request_cleanup(db, sid)
    real = run_purge.purge_run

    def purge(db_path, traces, session_id, **kwargs):
        if session_id == bad:
            raise RuntimeError("poisoned")
        return real(db_path, traces, session_id, **kwargs)

    monkeypatch.setattr(run_purge, "purge_run", purge)
    run_retention.finish_pending_cleanups()

    assert library.count("sessions", good) == 0 and library.count("sessions", bad) == 1
    assert run_leases.due_cleanups(db) == [bad]  # still queued for the next sweep


@pytest.mark.asyncio
async def test_releasing_leases_survives_a_failing_cleanup(library, monkeypatch):
    db = run_catalog_repo.db_path
    ids = [run_leases.acquire(db, library.seed(str(i))) for i in range(3)]
    monkeypatch.setattr(run_leases, "release", _failing_first(run_leases.release))

    await asyncio.to_thread(run_media.release, ids)

    assert _lease_rows(library) == 1  # only the one whose own release call failed


def _failing_first(real):
    calls = []

    def release(db_path, lease_id):
        calls.append(lease_id)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(db_path, lease_id)

    return release


def _lease_rows(library) -> int:
    import sqlite3

    with sqlite3.connect(library.db) as conn:
        return conn.execute("SELECT COUNT(*) FROM run_artifact_leases").fetchone()[0]


@pytest.mark.asyncio
async def test_a_partial_lease_acquire_releases_what_it_already_took(library, monkeypatch):
    owners = [library.seed(str(i)) for i in range(3)]
    real = run_leases.acquire
    calls = []

    def acquire(db_path, session_id):
        calls.append(session_id)
        if len(calls) == 3:
            raise RuntimeError("db locked")
        return real(db_path, session_id)

    monkeypatch.setattr(run_leases, "acquire", acquire)
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(run_media.lease, owners)

    assert _lease_rows(library) == 0
