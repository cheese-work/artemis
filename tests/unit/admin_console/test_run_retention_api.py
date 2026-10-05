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

"""Retention, pinning, deletion leases and the storage view at the server API seam (CHE-1093 A5)."""

import asyncio
from collections import namedtuple
import io
import os
import sqlite3
import threading
import zipfile

import pytest

from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import run_bundle, run_retention, run_storage

DiskUsage = namedtuple("DiskUsage", "total used free")


async def _run(client, sid):
    return await client.get(f"/api/runs/{sid}")


async def _exists(client, sid) -> bool:
    return (await _run(client, sid)).status_code == 200


def _populate(library, sid):
    """Everything a deletion has to clean up."""
    library.image(f"img-{sid[:8]}")
    library.step(sid, 1, pre=f"img-{sid[:8]}")
    library.write(sid, "stdout.log", "log")
    return library.video(sid)


async def _enable(admin, days=30):
    assert (await admin.put("/api/system/retention", json={"days": days})).status_code == 200
    assert (await admin.post("/api/system/retention/dry-run")).status_code == 200
    response = await admin.put("/api/system/retention", json={"enabled": True})
    assert response.status_code == 200, response.text


# -- defaults and the dry-run ---------------------------------------------------------


@pytest.mark.asyncio
async def test_enforcement_is_off_by_default_and_deletes_nothing(library, admin):
    old = library.seed("old", age_days=400)

    async with admin:
        settings = (await admin.get("/api/system/retention")).json()
        run_now = await admin.post("/api/system/retention/run")

        assert settings["enabled"] is False and settings["days"] == 30
        assert run_now.status_code == 409 and run_now.json()["error"] == "retention_disabled"
        assert await _exists(admin, old)


@pytest.mark.asyncio
async def test_dry_run_lists_exactly_what_would_go_and_deletes_nothing(library, admin):
    old = library.seed("old", age_days=40)
    older = library.seed("older", age_days=90)
    fresh = library.seed("fresh", age_days=5)
    pinned = library.seed("pinned", age_days=60, pinned=True)
    running = library.seed("running", status="running", age_days=60)
    queued = library.seed("queued", status="queued", age_days=60)
    uploading = library.seed("uploading", age_days=60)
    run_catalog_repo.set_recording_state(uploading, "rec-1", transfer="uploading")
    waiting = library.seed("waiting", age_days=60)
    run_catalog_repo.set_recording_state(waiting, "rec-1", transfer="waiting_for_computer")
    files = _populate(library, old)

    async with admin:
        report = (await admin.post("/api/system/retention/dry-run")).json()

        assert {r["session_id"] for r in report["would_delete"]} == {old, older}
        assert report["days"] == 30 and report["dry_run"] is True
        assert report["protected"] == {"pinned": 1, "live": 2, "pending_upload": 2}
        assert all(
            {"session_id", "prompt", "ended_at", "expires_at"} <= r.keys()
            for r in report["would_delete"]
        )
        for sid in (old, older, fresh, pinned, running, queued, uploading, waiting):
            assert await _exists(admin, sid)
    assert files.exists()


@pytest.mark.asyncio
async def test_dry_run_honours_a_shorter_window(library, admin):
    ten = library.seed("ten days", age_days=10)
    library.seed("two days", age_days=2)

    async with admin:
        report = (await admin.post("/api/system/retention/dry-run", params={"days": 7})).json()

    assert [r["session_id"] for r in report["would_delete"]] == [ten]


# -- enabling -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enabling_needs_a_dry_run_first_and_an_admin(library, admin, qa):
    library.seed("old", age_days=40)

    async with admin, qa:
        assert (await qa.put("/api/system/retention", json={"enabled": True})).status_code == 403
        early = await admin.put("/api/system/retention", json={"enabled": True})
        await admin.post("/api/system/retention/dry-run")
        ok = await admin.put("/api/system/retention", json={"enabled": True})
        changed = await admin.put("/api/system/retention", json={"days": 14})
        settings = (await admin.get("/api/system/retention")).json()

    assert early.status_code == 409 and early.json()["error"] == "dry_run_required"
    assert ok.status_code == 200 and ok.json()["enabled"] is True
    assert changed.status_code == 200
    assert settings["enabled"] is False and settings["days"] == 14  # a new window is re-reviewed


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, -1, 100000, "soon"])
async def test_retention_window_is_validated(library, admin, days):
    async with admin:
        response = await admin.put("/api/system/retention", json={"days": days})

    assert response.status_code == 422


# -- enforcement --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforcement_deletes_only_unprotected_expired_runs(library, admin):
    old = library.seed("old", age_days=40)
    fresh = library.seed("fresh", age_days=5)
    pinned = library.seed("pinned", age_days=60, pinned=True)
    running = library.seed("running", status="running", age_days=60)
    uploading = library.seed("uploading", age_days=60)
    run_catalog_repo.set_recording_state(uploading, "rec-1", transfer="uploading")
    files = {sid: _populate(library, sid) for sid in (old, fresh, pinned, running, uploading)}

    async with admin:
        await _enable(admin)
        result = (await admin.post("/api/system/retention/run")).json()

        assert result["deleted"] == [old] and result["deferred"] == []
        gone = await _run(admin, old)
        assert gone.status_code == 410 and gone.json()["reason"] == "retention"
        for sid in (fresh, pinned, running, uploading):
            assert await _exists(admin, sid)
    assert not files[old].exists() and not (library.traces / old).exists()
    assert library.count("steps", old) == 0 and library.count("sessions", old) == 0
    assert not (library.images / f"img-{old[:8]}.jpg").exists()
    for sid in (fresh, pinned, running, uploading):
        assert files[sid].exists() and library.count("steps", sid) == 1
        assert (library.images / f"img-{sid[:8]}.jpg").exists()


@pytest.mark.asyncio
async def test_a_pinned_run_survives_until_it_is_unpinned(library, admin, qa):
    sid = library.seed("keep", age_days=60)

    async with admin, qa:
        assert (await qa.post(f"/api/runs/{sid}/pin")).status_code == 200
        await _enable(admin)
        first = (await admin.post("/api/system/retention/run")).json()
        assert (await qa.post(f"/api/runs/{sid}/unpin")).status_code == 200
        second = (await admin.post("/api/system/retention/run")).json()

    assert first["deleted"] == [] and second["deleted"] == [sid]


@pytest.mark.asyncio
async def test_an_image_shared_with_a_surviving_run_is_kept(library, admin):
    old = library.seed("old", age_days=60)
    fresh = library.seed("fresh", age_days=1)
    shared = library.image("shared")
    library.step(old, 1, pre="shared")
    library.step(fresh, 1, pre="shared")

    async with admin:
        await _enable(admin)
        await admin.post("/api/system/retention/run")

    assert shared.exists()


# -- pin, delete, clear all ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_signed_in_user_can_pin_but_anonymous_cannot(library, qa, anonymous):
    sid = library.seed("pin me")

    async with qa, anonymous:
        denied = await anonymous.post(f"/api/runs/{sid}/pin")
        pinned = await qa.post(f"/api/runs/{sid}/pin")
        shown = (await _run(qa, sid)).json()
        unknown = await qa.post("/api/runs/00000000-0000-4000-8000-000000000000/pin")
        unpinned = await qa.post(f"/api/runs/{sid}/unpin")

    assert denied.status_code == 401
    assert pinned.status_code == 200 and pinned.json() == {"session_id": sid, "pinned": True}
    assert shown["pinned"] is True
    assert unknown.status_code == 404
    assert unpinned.json()["pinned"] is False


@pytest.mark.asyncio
async def test_only_admins_delete_a_run(library, admin, qa):
    sid = library.seed("doomed")
    files = _populate(library, sid)

    async with admin, qa:
        denied = await qa.post(f"/api/runs/{sid}/delete")
        assert denied.status_code == 403 and files.exists()
        done = await admin.post(f"/api/runs/{sid}/delete")
        removed = await _run(qa, sid)

    assert done.status_code == 200 and done.json() == {"session_id": sid, "cleanup": "done"}
    assert removed.status_code == 410 and removed.json()["reason"] == "admin_delete"
    assert not files.exists() and library.count("sessions", sid) == 0


@pytest.mark.asyncio
async def test_a_live_run_cannot_be_deleted(library, admin):
    sid = library.seed("busy", status="running")

    async with admin:
        response = await admin.post(f"/api/runs/{sid}/delete")
        legacy = await admin.post(f"/api/sessions/{sid}/delete")

        assert response.status_code == 409 and response.json()["error"] == "run_live"
        assert legacy.status_code == 409
        assert await _exists(admin, sid)


@pytest.mark.asyncio
async def test_the_existing_session_delete_route_uses_the_same_rules(library, admin, qa):
    sid = library.seed("legacy route")
    files = _populate(library, sid)

    async with admin, qa:
        assert (await qa.post(f"/api/sessions/{sid}/delete")).status_code == 403
        done = await admin.post(f"/api/sessions/{sid}/delete")

    assert done.status_code == 200
    assert not files.exists()
    assert run_catalog_repo.get_run(sid).removed["reason"] == "admin_delete"


@pytest.mark.asyncio
async def test_clear_all_needs_the_exact_count_and_an_admin(library, admin, qa):
    keep = library.seed("pinned", pinned=True)
    live = library.seed("live", status="running")
    doomed = [library.seed(f"run {i}") for i in range(3)]

    async with admin, qa:
        forbidden = await qa.post("/api/runs/clear", json={"confirm_count": 3})
        missing = await admin.post("/api/runs/clear", json={})
        wrong = await admin.post("/api/runs/clear", json={"confirm_count": 2})
        assert all([await _exists(admin, sid) for sid in doomed])
        done = await admin.post("/api/runs/clear", json={"confirm_count": 3})

        assert forbidden.status_code == 403
        assert missing.status_code == 422
        assert wrong.status_code == 409 and wrong.json()["expected_count"] == 3
        assert wrong.json()["error"] == "count_mismatch"
        assert done.status_code == 200 and sorted(done.json()["deleted"]) == sorted(doomed)
        assert done.json()["skipped"] == {"pinned": 1, "live": 1, "pending_upload": 0}
        assert not any([await _exists(admin, sid) for sid in doomed])
        assert await _exists(admin, keep) and await _exists(admin, live)


@pytest.mark.asyncio
async def test_the_legacy_cleanup_route_cannot_bypass_the_typed_count(library, admin):
    sid = library.seed("one")

    async with admin:
        bare = await admin.post("/api/cleanup")
        counted = await admin.post("/api/cleanup", json={"confirm_count": 1})

        assert bare.status_code in (409, 422)
        assert counted.status_code == 200
        assert not await _exists(admin, sid)


# -- download leases ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_during_a_bundle_build_defers_cleanup_until_the_lease_ends(
    library, admin, qa, monkeypatch
):
    sid = library.seed("downloading")
    files = _populate(library, sid)
    started, release = threading.Event(), threading.Event()
    real = run_bundle._build_zip

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_bundle, "_build_zip", slow)

    async with admin, qa:
        download = asyncio.create_task(qa.get(f"/api/runs/{sid}/bundle.zip"))
        assert await asyncio.to_thread(started.wait, 5)

        deleted = await admin.post(f"/api/runs/{sid}/delete")
        assert deleted.json() == {"session_id": sid, "cleanup": "deferred"}
        assert (await _run(qa, sid)).status_code == 410  # gone for new readers at once
        assert (await qa.get(f"/api/runs/{sid}/bundle.zip")).status_code == 410
        assert files.exists() and (library.traces / sid / "stdout.log").exists()  # still readable

        release.set()
        response = await download

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert response.status_code == 200 and archive.read("logs/stdout.log") == b"log"
    assert not files.exists() and not (library.traces / sid).exists()  # cleaned once released
    assert library.count("sessions", sid) == 0 and library.count("steps", sid) == 0


@pytest.mark.asyncio
async def test_retention_defers_runs_that_are_being_downloaded(library, admin, qa, monkeypatch):
    sid = library.seed("old but downloading", age_days=60)
    files = _populate(library, sid)
    started, release = threading.Event(), threading.Event()
    real = run_bundle._build_zip

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_bundle, "_build_zip", slow)

    async with admin, qa:
        await _enable(admin)
        download = asyncio.create_task(qa.get(f"/api/runs/{sid}/bundle.zip"))
        assert await asyncio.to_thread(started.wait, 5)
        result = (await admin.post("/api/system/retention/run")).json()
        assert result["deleted"] == [] and result["deferred"] == [sid]
        assert files.exists()
        release.set()
        assert (await download).status_code == 200

    assert not files.exists()


@pytest.mark.asyncio
async def test_a_deferred_cleanup_survives_a_restart(library, admin, monkeypatch):
    from apps.admin_console.services import run_leases

    sid = library.seed("crashy")
    files = _populate(library, sid)
    lease = run_leases.acquire(run_catalog_repo.db_path, sid)
    async with admin:
        assert (await admin.post(f"/api/runs/{sid}/delete")).json()["cleanup"] == "deferred"
    assert files.exists()

    # The server died holding the lease: it expires, and startup finishes the cleanup.
    monkeypatch.setattr(run_leases, "LEASE_TTL_SECONDS", -1)
    from apps.admin_console.services import run_retention

    run_retention.finish_pending_cleanups()

    assert lease and not files.exists() and library.count("sessions", sid) == 0


# -- storage view -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_view_reports_usage_counts_and_free_disk(library, qa, monkeypatch):
    monkeypatch.setattr(run_storage, "disk_usage", lambda _path: DiskUsage(1000, 400, 600))
    a = library.seed("a")
    b = library.seed("b", pinned=True)
    library.write(a, "stdout.log", "x" * 1000)
    library.video(b, b"v" * 2000)
    library.seed("gone")
    run_catalog_repo.tombstone(
        next(r["session_id"] for r in run_catalog_repo.list_runs().runs if r["prompt"] == "gone"),
        "admin_delete",
    )

    async with qa:
        body = (await qa.get("/api/system/storage")).json()

    assert body["run_count"] == 2 and body["pinned_count"] == 1
    assert body["clearable_count"] == 1  # what "Clear all" would delete: the unpinned run
    assert body["usage_bytes"] >= 3000 and body["pinned_bytes"] >= 2000
    assert body["disk"] == {"total_bytes": 1000, "free_bytes": 600, "free_percent": 60.0}
    assert body["warnings"] == []
    assert body["retention"]["enabled"] is False and body["retention"]["days"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("free", "level"), [(19, "warning"), (10, "warning"), (9, "critical"), (0, "critical")]
)
async def test_storage_warns_below_twenty_and_ten_percent_free(
    library, qa, monkeypatch, free, level
):
    monkeypatch.setattr(run_storage, "disk_usage", lambda _path: DiskUsage(100, 100 - free, free))

    async with qa:
        warnings = (await qa.get("/api/system/storage")).json()["warnings"]

    assert [(w["level"], w["code"]) for w in warnings] == [(level, "low_disk")]


@pytest.mark.asyncio
async def test_storage_view_turns_critical_after_an_insufficient_storage_response(
    library, qa, monkeypatch
):
    monkeypatch.setattr(run_storage, "disk_usage", lambda _path: DiskUsage(100, 10, 90))
    run_storage.note_insufficient_storage(run_catalog_repo.db_path)

    async with qa:
        warnings = (await qa.get("/api/system/storage")).json()["warnings"]

    assert [(w["level"], w["code"]) for w in warnings] == [("critical", "insufficient_storage")]


@pytest.mark.asyncio
async def test_a_full_disk_while_bundling_is_a_507_and_is_remembered(library, qa, monkeypatch):
    import errno

    sid = library.seed("full")
    monkeypatch.setattr(run_storage, "disk_usage", lambda _path: DiskUsage(100, 10, 90))

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(run_bundle, "_build_zip", full)

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")
        warnings = (await qa.get("/api/system/storage")).json()["warnings"]

    assert response.status_code == 507
    assert warnings[0]["code"] == "insufficient_storage"


# -- review round 2: intermediate symlinks, delete-time guards -------------------------------


def _mutate(library, sid, how):
    with sqlite3.connect(library.db) as other:
        if how == "live":
            other.execute("UPDATE sessions SET status = 'running' WHERE session_id = ?", (sid,))
        elif how == "pinned":
            other.execute("UPDATE run_meta SET pinned = 1 WHERE session_id = ?", (sid,))
        else:
            other.execute(
                "INSERT INTO run_recording_state (session_id, recording_id, transfer) "
                "VALUES (?, 'rec-1', 'uploading')",
                (sid,),
            )


def _flip(monkeypatch, library, sid, how):
    """After the retention/clear selection reads its rows, change the run behind its back."""
    real = run_retention._runs

    def selected_then_changed(conn):
        rows = real(conn)
        _mutate(library, sid, how)
        return rows

    monkeypatch.setattr(run_retention, "_runs", selected_then_changed)


def _untouched(library, sid, files):
    with sqlite3.connect(library.db) as conn:
        meta = conn.execute(
            "SELECT deleted_at FROM run_meta WHERE session_id = ?", (sid,)
        ).fetchone()
    assert meta[0] is None  # not tombstoned
    assert files.exists() and library.count("sessions", sid) == 1 and library.count("steps", sid) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["live", "pinned", "pending_upload"])
async def test_retention_rechecks_guards_when_it_deletes(library, admin, monkeypatch, how):
    sid = library.seed("old", age_days=60)
    files = _populate(library, sid)

    async with admin:
        await _enable(admin)
        _flip(monkeypatch, library, sid, how)
        result = (await admin.post("/api/system/retention/run")).json()

    assert result["deleted"] == [] and result["deferred"] == []
    _untouched(library, sid, files)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["live", "pinned", "pending_upload"])
async def test_clear_all_rechecks_guards_when_it_deletes(library, admin, monkeypatch, how):
    sid = library.seed("victim")
    files = _populate(library, sid)

    async with admin:
        _flip(monkeypatch, library, sid, how)
        result = await admin.post("/api/runs/clear", json={"confirm_count": 1})

    assert result.status_code == 200 and result.json()["deleted"] == []
    _untouched(library, sid, files)


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["live", "pending_upload"])
async def test_admin_delete_never_removes_a_live_or_uploading_run(library, admin, monkeypatch, how):
    sid = library.seed("busy")
    files = _populate(library, sid)

    # The state changes after the handler's own check, before the delete itself.
    real = run_retention._lookup

    def lookup_then_change(session_id):
        row = real(session_id)
        _mutate(library, sid, how)
        return row

    monkeypatch.setattr(run_retention, "_lookup", lookup_then_change)

    async with admin:
        response = await admin.post(f"/api/runs/{sid}/delete")

    assert response.status_code == 409
    _untouched(library, sid, files)


@pytest.mark.asyncio
async def test_admin_delete_refuses_a_run_with_a_pending_upload(library, admin):
    sid = library.seed("uploading")
    run_catalog_repo.set_recording_state(sid, "rec-1", transfer="uploading")

    async with admin:
        response = await admin.post(f"/api/runs/{sid}/delete")

    assert response.status_code == 409 and response.json()["error"] == "run_pending_upload"


@pytest.mark.asyncio
async def test_a_symlinked_parent_of_a_recording_never_deletes_outside_storage(library, admin, tmp_path):
    sid = library.seed("linked recording", age_days=1)
    outside = tmp_path / "outside" / "sub"
    outside.mkdir(parents=True)
    precious = outside / "video.mp4"
    precious.write_bytes(b"PRECIOUS")
    os.symlink(tmp_path / "outside", library.traces / "link")
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "INSERT INTO video_recordings (video_id, session_id, local_video_path, status) "
            "VALUES ('v1', ?, ?, 'ready')",
            (sid, str(library.traces / "link" / "sub" / "video.mp4")),
        )

    async with admin:
        response = await admin.post(f"/api/runs/{sid}/delete")

    assert response.status_code == 200
    assert precious.read_bytes() == b"PRECIOUS"
    assert (library.traces / "link").is_symlink()
