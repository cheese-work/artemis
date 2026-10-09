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

"""Run bundles and trace downloads at the server API seam (CHE-1093 A5)."""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import zipfile

from PIL import Image
import pytest

from apps.admin_console.services import run_bundle

SECRET_BEARER = "Bearer sk-live-abcdefghijklmnopqrstuvwx"


def _zip(response) -> zipfile.ZipFile:
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _text_entries(archive: zipfile.ZipFile) -> dict[str, str]:
    return {
        info.filename: archive.read(info).decode("utf-8", "replace")
        for info in archive.infolist()
        if not info.filename.startswith(("images/", "video/"))
    }


def _full_run(library):
    sid = library.seed("Open Settings and type the password hunter2")
    library.image("pre1", b"PRE-IMAGE password=hunter2")
    library.image("post1", b"POST-IMAGE")
    library.step(
        sid,
        1,
        pre="pre1",
        post="post1",
        action={"type": "input_text", "args": {"note": "password=hunter2"}},
        trace_payload={"headers": {"Authorization": SECRET_BEARER}, "log": ["token=abc12345"]},
    )
    library.video(sid, b"VIDEO password=hunter2")
    library.write(sid, "stdout.log", "starting\npassword=hunter2\n")
    library.write(sid, "stderr.log", f"oops {SECRET_BEARER}\n")
    library.write(sid, "notes/finding.md", "api_key=abcdef123456")
    library.write(sid, "notes/task_plan.md", "1. type password: hunter2")
    library.write(
        sid, "check_ledger.jsonl", json.dumps({"verdict": "ok", "secret": "s3cr3t"}) + "\n"
    )
    return sid


# -- content ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_holds_prompt_steps_images_video_and_logs(library, qa):
    sid = _full_run(library)

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    archive = _zip(response)
    names = set(archive.namelist())
    assert {
        "prompt.txt",
        "steps.json",
        "manifest.json",
        "logs/stdout.log",
        "logs/stderr.log",
    } <= names
    assert {"images/pre1.jpg", "images/post1.jpg"} <= names
    assert any(n.startswith("video/") and n.endswith("recording.mp4") for n in names)
    assert {"notes/finding.md", "notes/task_plan.md", "checks/check_ledger.jsonl"} <= names
    assert response.headers["content-type"] == "application/zip"
    assert int(response.headers["content-length"]) == len(response.content)
    assert (
        "bundle" in response.headers["content-disposition"]
        and sid[:8] in (response.headers["content-disposition"])
    )
    assert json.loads(archive.read("steps.json"))[0]["step_number"] == 1


@pytest.mark.asyncio
async def test_text_artifacts_are_redacted_and_media_is_not(library, qa):
    sid = _full_run(library)

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    for name, text in _text_entries(archive).items():
        for secret in ("hunter2", "abcdefghijklmnopqrstuvwx", "abc12345", "abcdef123456", "s3cr3t"):
            assert secret not in text, f"{secret} leaked through {name}"
    assert "[REDACTED]" in archive.read("logs/stdout.log").decode()
    assert archive.read("images/pre1.jpg") == b"PRE-IMAGE password=hunter2"  # media: untouched
    video = next(n for n in archive.namelist() if n.startswith("video/"))
    assert archive.read(video) == b"VIDEO password=hunter2"


@pytest.mark.asyncio
async def test_prompt_is_redacted_too(library, qa):
    sid = library.seed("log in with password=hunter2")

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    assert "hunter2" not in archive.read("prompt.txt").decode()
    assert "log in with" in archive.read("prompt.txt").decode()


@pytest.mark.asyncio
async def test_bundles_remove_inline_images_and_references_from_every_text_artifact(library, qa):
    picture = io.BytesIO()
    Image.new("RGB", (8, 6), (200, 30, 30)).save(picture, "PNG")
    encoded = base64.b64encode(picture.getvalue()).decode()
    uri = f"data:image/png;base64,{encoded}"
    sid = library.seed(f'password="zebra7secret" {uri}')
    library.step(sid, 1, action={"image": uri, "ref": "image://private-hash"})
    library.write(sid, "notes/goal.md", f"{uri} <ImageRef:private-hash>")
    library.write(sid, "stdout.log", f"{encoded}\n")
    library.write(
        sid,
        "check_ledger.jsonl",
        json.dumps({"inline_data": {"mime_type": "image/png", "data": encoded}}) + "\n",
    )

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    for name, text in _text_entries(archive).items():
        assert encoded not in text, name
        assert "private-hash" not in text, name
        assert "zebra7secret" not in text, name
        assert "data:image" not in text, name


@pytest.mark.asyncio
async def test_bundle_refuses_legacy_inline_cache_even_if_named_as_a_recording(library, qa):
    sid = library.seed("private attachment")
    content = b"private-goal-image"
    digest = hashlib.sha256(content).hexdigest()
    library.video_in(sid, "images", f"{digest}.jpg", content)

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    assert not any(content in archive.read(name) for name in archive.namelist())
    assert "prompt_attachment" in archive.read("manifest.json").decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", ["goal_images", "GOAL_IMAGES"])
async def test_bundle_does_not_export_goal_attachments_as_recordings(library, qa, folder):
    sid = library.seed("prompt with a private attachment")
    attachment = library.video_in(sid, f"{sid}/{folder}", "0.png", b"PRIVATE-GOAL-IMAGE")

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    assert not any(b"PRIVATE-GOAL-IMAGE" in archive.read(name) for name in archive.namelist())
    assert {"name": f"video/{attachment.name}", "reason": "prompt_attachment"} in json.loads(
        archive.read("manifest.json")
    )["skipped"]


# -- containment -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_symlinked_artifacts_are_rejected_not_followed(library, qa, tmp_path):
    sid = library.seed("symlinks")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.md").write_text("TOP-SECRET-FILE", encoding="utf-8")
    (outside / "stdout.log").write_text("TOP-SECRET-LOG", encoding="utf-8")
    (outside / "evil.jpg").write_bytes(b"TOP-SECRET-IMAGE")
    notes = library.traces / sid / "notes"
    notes.mkdir()
    os.symlink(outside / "stolen.md", notes / "stolen.md")
    os.symlink(outside / "stdout.log", library.traces / sid / "stdout.log")
    library.images.mkdir(exist_ok=True)
    os.symlink(outside / "evil.jpg", library.images / "evil.jpg")
    library.step(sid, 1, pre="evil")
    linked_dir = library.traces / "elsewhere"
    os.symlink(outside, linked_dir)
    video = library.traces / sid / "recording.mp4"
    os.symlink(outside / "evil.jpg", video)
    with sqlite3.connect(library.db) as conn:
        conn.execute(
            "INSERT INTO video_recordings (video_id, session_id, local_video_path, status) "
            "VALUES ('v1', ?, ?, 'ready')",
            (sid, str(video)),
        )

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    archive = _zip(response)
    blob = b"".join(archive.read(n) for n in archive.namelist())
    assert b"TOP-SECRET" not in blob
    assert not {"notes/stolen.md", "logs/stdout.log", "images/evil.jpg"} & set(archive.namelist())
    skipped = json.loads(archive.read("manifest.json"))["skipped"]
    assert {entry["reason"] for entry in skipped} == {"symlink"}
    assert len(skipped) >= 4


@pytest.mark.asyncio
async def test_symlinked_session_directory_is_not_followed(library, qa, tmp_path):
    sid = library.seed("linked dir")
    (library.traces / sid).rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stdout.log").write_text("TOP-SECRET-LOG", encoding="utf-8")
    os.symlink(outside, library.traces / sid)

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    # The id check already refuses a folder whose real path leaves traces.
    assert response.status_code == 400
    assert b"TOP-SECRET" not in response.content


@pytest.mark.asyncio
async def test_dot_dot_names_from_the_database_cannot_escape(library, qa):
    sid = library.seed("traversal")
    (library.traces / "outside.jpg").write_bytes(b"OUTSIDE-IMAGES-DIR")
    library.step(sid, 1, pre="../outside", post="../../etc/passwd")

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    assert b"OUTSIDE-IMAGES-DIR" not in b"".join(archive.read(n) for n in archive.namelist())
    for name in archive.namelist():
        assert not name.startswith("/") and ".." not in name.split("/"), name
    assert any(
        e["reason"] == "outside_storage"
        for e in json.loads(archive.read("manifest.json"))["skipped"]
    )


# -- cap, concurrency, cleanup ------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_over_the_size_cap_is_413_and_leaves_no_temp_file(
    library, qa, monkeypatch, tmp_path
):
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp_dir))
    monkeypatch.setattr(run_bundle, "MAX_BUNDLE_BYTES", 100)
    sid = library.seed("big")
    library.video(sid, b"x" * 500)

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    assert response.status_code == 413
    assert response.json()["error"] == "bundle_too_large"
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_expanding_redacted_text_cannot_slip_past_the_cap(library, qa, monkeypatch, tmp_path):
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp_dir))
    sid = library.seed("small sources")
    library.write(sid, "stdout.log", "ok " * 2000)
    monkeypatch.setattr(
        run_bundle, "MAX_BUNDLE_BYTES", 1500
    )  # compressed result is tiny; raw is not

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    assert response.status_code == 413
    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_temp_file_is_removed_after_a_successful_download(library, qa, monkeypatch, tmp_path):
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp_dir))
    sid = _full_run(library)

    async with qa:
        assert (await qa.get(f"/api/runs/{sid}/bundle.zip")).status_code == 200

    assert list(temp_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_at_most_two_bundles_build_at_once(library, qa, monkeypatch):
    sid = _full_run(library)
    started = threading.Semaphore(0)
    release = threading.Event()
    real = run_bundle._build_zip

    def slow(*args, **kwargs):
        started.release()
        assert release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_bundle, "_build_zip", slow)

    async with qa:
        first = asyncio.create_task(qa.get(f"/api/runs/{sid}/bundle.zip"))
        second = asyncio.create_task(qa.get(f"/api/runs/{sid}/bundle.zip"))
        for _ in range(2):
            assert await asyncio.to_thread(started.acquire, True, 5)
        third = await qa.get(f"/api/runs/{sid}/bundle.zip")
        release.set()
        done = [await first, await second]
        after = await qa.get(f"/api/runs/{sid}/bundle.zip")

    assert third.status_code == 429
    assert third.json()["error"] == "bundle_busy"
    assert int(third.headers["retry-after"]) >= 1
    assert [r.status_code for r in done] == [200, 200]
    assert after.status_code == 200  # the slot came back


# -- lookup errors, logging ---------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_removed_and_malformed_ids(library, qa):
    gone = library.seed("gone")
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

    run_catalog_repo.tombstone(gone, "admin_delete")

    async with qa:
        unknown = await qa.get("/api/runs/00000000-0000-4000-8000-000000000000/bundle.zip")
        removed = await qa.get(f"/api/runs/{gone}/bundle.zip")
        malformed = await qa.get("/api/runs/bad%20id/bundle.zip")
        dots = await qa.get("/api/runs/%2E%2E/bundle.zip")

    assert unknown.status_code == 404
    assert removed.status_code == 410 and removed.json()["reason"] == "admin_delete"
    assert malformed.status_code == 400
    assert dots.status_code in (400, 404)


@pytest.mark.asyncio
async def test_legacy_run_ids_still_bundle(library, qa):
    sid = library.seed("legacy", sid="web_legacy-1.2")
    library.write(sid, "stdout.log", "hello")

    async with qa:
        archive = _zip(await qa.get(f"/api/runs/{sid}/bundle.zip"))

    assert archive.read("logs/stdout.log") == b"hello"


@pytest.mark.asyncio
async def test_bundle_download_emits_a_structured_event(library, qa, caplog):
    sid = _full_run(library)

    with caplog.at_level(logging.INFO):
        async with qa:
            response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    event = next(
        r.getMessage() for r in caplog.records if "event=bundle_download" in r.getMessage()
    )
    assert f"session_id={sid}" in event
    assert "requester=qa@example.com" in event
    assert f"bytes={len(response.content)}" in event


# -- trace download -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_download_passes_through_the_redactor(library, qa):
    sid = library.seed("traces")
    payload = {
        "request": {"headers": {"Authorization": SECRET_BEARER}},
        "messages": [{"content": "type password=hunter2 then tap"}],
        "nested": json.dumps({"api_key": "abcdef123456"}),
        "ok": 3,
    }
    trace_id = library.trace(sid, json.dumps(payload))

    async with qa:
        response = await qa.get(f"/api/traces/{trace_id}/download")

    assert response.status_code == 200
    for secret in ("hunter2", "abcdefghijklmnopqrstuvwx", "abcdef123456"):
        assert secret not in response.text
    body = json.loads(response.text)  # still valid JSON
    assert body["ok"] == 3 and "type" in body["messages"][0]["content"]
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_trace_download_of_non_json_payload_is_redacted_text(library, qa):
    sid = library.seed("traces")
    trace_id = library.trace(sid, "plain text with password=hunter2")

    async with qa:
        response = await qa.get(f"/api/traces/{trace_id}/download")

    assert "hunter2" not in response.text and "plain text with" in response.text


# -- legacy id validation on reads ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["plan", "notes", "checks"])
async def test_session_file_reads_reject_unsafe_ids(library, qa, suffix):
    (library.traces.parent / "notes").mkdir()
    (library.traces.parent / "notes" / "task_plan.md").write_text("OUTSIDE", encoding="utf-8")

    async with qa:
        dots = await qa.get(f"/api/sessions/%2E%2E/{suffix}")
        spaces = await qa.get(f"/api/sessions/bad%20id/{suffix}")

    assert dots.status_code == 400 and spaces.status_code == 400
    assert "OUTSIDE" not in dots.text


# -- open-time containment, lease scope (review round 2) ----------------------------------


def _swap_dir_for_symlink(real: Path, outside: Path) -> None:
    moved = real.with_name(real.name + "-moved")
    real.rename(moved)
    os.symlink(outside, real)


@pytest.mark.asyncio
async def test_a_parent_swapped_to_a_symlink_after_listing_is_not_followed(
    library, qa, tmp_path, monkeypatch
):
    sid = library.seed("swap")
    library.write(sid, "notes/finding.md", "INSIDE-NOTE")
    video = library.video(sid, b"INSIDE-VIDEO")
    outside = tmp_path / "outside"
    (outside).mkdir()
    (outside / "finding.md").write_text("TOP-SECRET-NOTE", encoding="utf-8")
    (outside / "recording.mp4").write_bytes(b"TOP-SECRET-VIDEO")
    real = run_bundle._build_zip

    def swap_then_build(manifest, dest):
        # The manifest was resolved safely; now the parents are replaced by links.
        _swap_dir_for_symlink(library.traces / sid / "notes", outside)
        _swap_dir_for_symlink(video.parent, outside)
        return real(manifest, dest)

    monkeypatch.setattr(run_bundle, "_build_zip", swap_then_build)

    async with qa:
        response = await qa.get(f"/api/runs/{sid}/bundle.zip")

    archive = _zip(response)
    assert b"TOP-SECRET" not in b"".join(archive.read(n) for n in archive.namelist())
    assert "notes/finding.md" not in archive.namelist()
    listing = json.loads(archive.read("manifest.json"))
    assert {e["reason"] for e in listing["skipped"]} == {"symlink"}
    assert "notes/finding.md" not in {e["name"] for e in listing["entries"]}


@pytest.mark.asyncio
async def test_unresolved_ids_take_no_lease(library, qa, monkeypatch):
    from apps.admin_console.services import run_leases

    library.seed("one", sid="aaaaaaaa-0000-4000-8000-000000000001")
    library.seed("two", sid="aaaaaaaa-0000-4000-8000-000000000002")
    leased: list[str] = []
    real = run_leases.acquire
    monkeypatch.setattr(run_leases, "acquire", lambda db, sid: leased.append(sid) or real(db, sid))

    async with qa:
        unknown = await qa.get("/api/runs/00000000-0000-4000-8000-000000000000/bundle.zip")
        ambiguous = await qa.get("/api/runs/aaaaaaaa/bundle.zip")
        malformed = await qa.get("/api/runs/bad%20id/bundle.zip")

    assert (unknown.status_code, ambiguous.status_code, malformed.status_code) == (404, 409, 400)
    assert leased == []


@pytest.mark.asyncio
async def test_a_prefix_download_leases_the_full_id(library, qa, monkeypatch):
    sid = library.seed("prefixed", sid="bbbbbbbb-0000-4000-8000-000000000001")
    seen: list[str] = []
    real = run_bundle._build_zip

    def peek(manifest, dest):
        with sqlite3.connect(library.db) as conn:
            seen.extend(r[0] for r in conn.execute("SELECT session_id FROM run_artifact_leases"))
        return real(manifest, dest)

    monkeypatch.setattr(run_bundle, "_build_zip", peek)

    async with qa:
        response = await qa.get("/api/runs/bbbbbbbb/bundle.zip")

    assert response.status_code == 200 and seen == [sid]
