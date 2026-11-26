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

"""Retention expires a run's media, not the run, when the run has notes (CHE-1467 stage 1)."""

import sqlite3

import pytest

from apps.admin_console.core.access_control import AccessIdentity, public_tier
from apps.admin_console.database.repositories import annotation_repository as notes
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.routers.run_annotations import initialize_notes
from apps.admin_console.server import app
from apps.admin_console.services import run_leases, run_retention
from tests.unit.admin_console.conftest import make_client


@pytest.fixture(autouse=True)
def _notes_schema(library):
    initialize_notes()


@pytest.fixture
def admin(monkeypatch):
    """An admin with a verified issuer and subject, which writing notes requires."""
    identity = AccessIdentity(
        "admin@example.com", True, "cloudflare", issuer="https://access.example.com", subject="sub"
    )
    monkeypatch.setitem(app.dependency_overrides, public_tier, lambda: identity)
    return make_client("admin")


async def _enable(admin, days=30):
    assert (await admin.put("/api/system/retention", json={"days": days})).status_code == 200
    assert (await admin.post("/api/system/retention/dry-run")).status_code == 200
    assert (await admin.put("/api/system/retention", json={"enabled": True})).status_code == 200


async def _sweep(admin) -> dict:
    response = await admin.post("/api/system/retention/run")
    assert response.status_code == 200, response.text
    return response.json()


def _run(library, *, note: str | None = "comment", **kwargs):
    """An expired run with media, a log and (optionally) a note: (run, step, files)."""
    sid = library.seed(age_days=kwargs.pop("age_days", 40), **kwargs)
    shot = library.image(f"img-{sid[:8]}")
    step = library.step(sid, 1, pre=f"img-{sid[:8]}")
    video = library.video(sid)
    log = library.write(sid, "stdout.log", "log")
    if note == "comment":
        with sqlite3.connect(library.db) as conn:
            notes.add_comment(conn, sid, "a comment", "qa@example.com")
    return sid, step, {"shot": shot, "video": video, "log": log}


async def _annotate(admin, sid, anchor):
    response = await admin.post(
        f"/api/runs/{sid}/annotations", json={"anchor": anchor, "body": "note"}
    )
    assert response.status_code == 201, response.text
    return response.json()["annotation_id"]


async def _evidence(admin, sid, annotation_id):
    return await admin.get(f"/api/runs/{sid}/annotations/{annotation_id}/evidence")


def _count(library, sql, *args) -> int:
    with sqlite3.connect(library.db) as conn:
        return conn.execute(sql, args).fetchone()[0]


def _recording_id(library, sid) -> str:
    with sqlite3.connect(library.db) as conn:
        return conn.execute(
            "SELECT video_id FROM video_recordings WHERE session_id = ?", (sid,)
        ).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["annotation", "comment"])
async def test_noted_run_loses_its_media_and_keeps_the_run(library, admin, note):
    sid, step, files = _run(library, note=None)
    async with admin:
        annotation = await _annotate(admin, sid, {"kind": "step", "step_id": step})
        if note == "comment":
            with sqlite3.connect(library.db) as conn:
                conn.execute("DELETE FROM run_annotations")
                notes.add_comment(conn, sid, "a comment", "qa@example.com")
        await _enable(admin)
        report = await _sweep(admin)

        assert report["media_expired"] == [sid] and sid not in report["deleted"]
        assert not files["video"].exists() and not files["shot"].exists()
        assert files["log"].exists()
        assert (
            _count(library, "SELECT COUNT(*) FROM images WHERE image_name = ?", f"img-{sid[:8]}")
            == 0
        )
        assert _count(library, "SELECT COUNT(*) FROM steps WHERE session_id = ?", sid) == 1
        assert _count(library, "SELECT COUNT(*) FROM sessions WHERE session_id = ?", sid) == 1
        assert (
            _count(
                library,
                "SELECT COUNT(*) FROM run_meta WHERE session_id = ? AND deleted_at IS NULL",
                sid,
            )
            == 1
        )
        assert (
            _count(library, "SELECT COUNT(*) FROM evidence_tombstones WHERE session_id = ?", sid)
            == 2
        )
        assert (await admin.get(f"/api/runs/{sid}")).status_code == 200
        if note == "annotation":
            evidence = await _evidence(admin, sid, annotation)
            assert evidence.status_code == 410 and evidence.json()["code"] == "evidence_expired"
        else:
            assert (
                _count(library, "SELECT COUNT(*) FROM run_comments WHERE session_id = ?", sid) == 1
            )


@pytest.mark.asyncio
async def test_run_without_notes_is_deleted_as_before(library, admin):
    plain, _, plain_files = _run(library, note=None)
    noted, _, noted_files = _run(library, note="comment")
    async with admin:
        await _enable(admin)
        report = await _sweep(admin)

        assert report["deleted"] == [plain] and report["media_expired"] == [noted]
        assert (await admin.get(f"/api/runs/{plain}")).status_code == 410
    assert not plain_files["log"].exists() and not plain_files["video"].exists()
    assert noted_files["log"].exists()


@pytest.mark.asyncio
async def test_notes_saved_just_before_and_just_after_expiry_both_survive(library, admin):
    sid, step, _ = _run(library, note=None)
    recording = _recording_id(library, sid)
    async with admin:
        before = await _annotate(admin, sid, {"kind": "step", "step_id": step})
        await _enable(admin)
        assert (await _sweep(admin))["media_expired"] == [sid]
        after_step = await _annotate(admin, sid, {"kind": "step", "step_id": step})
        after_rec = await _annotate(
            admin, sid, {"kind": "recording", "recording_id": recording, "offset_ms": 0}
        )

        assert (
            _count(library, "SELECT COUNT(*) FROM run_annotations WHERE session_id = ?", sid) == 3
        )
        for annotation in (before, after_step, after_rec):
            evidence = await _evidence(admin, sid, annotation)
            assert evidence.status_code == 410 and evidence.json()["code"] == "evidence_expired"


@pytest.mark.asyncio
async def test_a_second_sweep_does_not_expire_the_run_again_or_delete_it(library, admin):
    sid, _, _ = _run(library)
    async with admin:
        await _enable(admin)
        assert (await _sweep(admin))["media_expired"] == [sid]
        stamp = _count(library, "SELECT expired_at FROM run_media_expiry WHERE session_id = ?", sid)
        dry = (await admin.post("/api/system/retention/dry-run")).json()
        second = await _sweep(admin)

        assert dry["would_expire_media"] == [] and dry["would_delete"] == []
        assert second["media_expired"] == [] and second["deleted"] == []
        assert (
            _count(library, "SELECT expired_at FROM run_media_expiry WHERE session_id = ?", sid)
            == stamp
        )
        assert (await admin.get(f"/api/runs/{sid}")).status_code == 200


@pytest.mark.asyncio
async def test_protected_runs_with_notes_are_not_changed(library, admin):
    pinned, _, pinned_files = _run(library, pinned=True)
    live, _, live_files = _run(library, status="running")
    uploading, _, uploading_files = _run(library)
    run_catalog_repo.set_recording_state(uploading, "rec-1", transfer="uploading")
    async with admin:
        await _enable(admin)
        report = await _sweep(admin)

        assert report["media_expired"] == [] and report["deleted"] == []
        for files in (pinned_files, live_files, uploading_files):
            assert files["video"].exists() and files["shot"].exists()
        for sid in (pinned, live, uploading):
            assert (
                _count(library, "SELECT COUNT(*) FROM run_media_expiry WHERE session_id = ?", sid)
                == 0
            )
            assert (await admin.get(f"/api/runs/{sid}")).status_code == 200


def test_guards_are_checked_again_inside_the_expiry_transaction(library):
    pinned, _, files = _run(library, pinned=True)
    assert run_retention._expire_media(pinned) is None
    assert files["video"].exists()
    plain, _, plain_files = _run(library, note=None)
    assert run_retention._expire_media(plain) is None  # no notes: not a media-expiry run
    assert plain_files["video"].exists()


@pytest.mark.asyncio
async def test_dry_run_lists_media_expiry_apart_from_deletion(library, admin):
    noted, _, files = _run(library)
    plain, _, _ = _run(library, note=None)
    async with admin:
        report = (await admin.post("/api/system/retention/dry-run")).json()

    assert [r["session_id"] for r in report["would_expire_media"]] == [noted]
    assert [r["session_id"] for r in report["would_delete"]] == [plain]
    assert files["video"].exists()


@pytest.mark.asyncio
async def test_active_download_lease_defers_expiry_to_the_next_sweep(library, admin):
    sid, _, files = _run(library)
    lease = run_leases.acquire(library.db, sid)
    async with admin:
        await _enable(admin)
        assert (await _sweep(admin))["media_expired"] == []
        assert files["video"].exists()
        run_leases.release(library.db, lease)
        assert (await _sweep(admin))["media_expired"] == [sid]
    assert not files["video"].exists()


@pytest.mark.asyncio
async def test_a_failed_file_removal_is_retried_by_the_next_sweep(library, admin, monkeypatch):
    sid, _, files = _run(library)
    real = run_retention.run_purge.remove_media
    monkeypatch.setattr(
        run_retention.run_purge, "remove_media", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    )
    async with admin:
        await _enable(admin)
        await _sweep(admin)
        assert files["video"].exists()  # expiry recorded, files still there
        monkeypatch.setattr(run_retention.run_purge, "remove_media", real)
        await _sweep(admin)
        assert not files["video"].exists() and files["log"].exists()
        assert (await _sweep(admin))["media_expired"] == []


def test_old_binary_deletion_leaves_no_expiry_row_after_the_next_migration(library):
    sid, _, _ = _run(library)
    assert run_retention._expire_media(sid) == "done"
    with sqlite3.connect(library.db) as conn:  # the previous binary deletes whole runs
        conn.execute("DELETE FROM steps WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
    notes.migrate(library.db)
    assert _count(library, "SELECT COUNT(*) FROM run_media_expiry") == 0
