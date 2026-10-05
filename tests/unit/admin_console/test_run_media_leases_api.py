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

"""Single-file downloads hold a lease on the runs that own the file (CHE-1093 A5, round 3)."""

import asyncio

from fastapi.responses import FileResponse
import pytest

from apps.admin_console.routers import media as media_router
from apps.admin_console.services import run_media


@pytest.fixture
def media_roots(library, monkeypatch):
    monkeypatch.setattr(media_router, "_allowed_media_roots", lambda: [library.traces.resolve()])
    monkeypatch.setattr(media_router, "IMAGES_DIR", library.images)


@pytest.fixture
def held_response(monkeypatch):
    """Hold every file response open until the test releases it."""
    started, release = asyncio.Event(), asyncio.Event()
    real = FileResponse.__call__

    async def slow(self, scope, receive, send):
        started.set()
        await release.wait()
        return await real(self, scope, receive, send)

    monkeypatch.setattr(FileResponse, "__call__", slow)
    return started, release


@pytest.mark.asyncio
async def test_delete_during_a_video_download_defers_cleanup_until_it_ends(
    library, admin, qa, media_roots, held_response
):
    started, release = held_response
    sid = library.seed("watching")
    video = library.video(sid, b"VIDEO")
    rel = f"{video.parent.name}/{video.name}"

    async with admin, qa:
        download = asyncio.create_task(qa.get(f"/videos/{rel}"))
        await asyncio.wait_for(started.wait(), 5)

        deleted = await admin.post(f"/api/runs/{sid}/delete")
        assert deleted.json()["cleanup"] == "deferred"
        assert video.exists()  # readable while the response is open

        release.set()
        response = await download

    assert response.status_code == 200 and response.content == b"VIDEO"
    assert not video.exists() and library.count("sessions", sid) == 0


@pytest.mark.asyncio
async def test_delete_during_an_image_download_defers_cleanup(
    library, admin, qa, media_roots, held_response
):
    started, release = held_response
    sid = library.seed("looking")
    image = library.image("shot1", b"IMG")
    library.step(sid, 1, pre="shot1")

    async with admin, qa:
        download = asyncio.create_task(qa.get("/images/shot1"))
        await asyncio.wait_for(started.wait(), 5)
        assert (await admin.post(f"/api/runs/{sid}/delete")).json()["cleanup"] == "deferred"
        assert image.exists()
        release.set()
        response = await download

    assert response.status_code == 200 and response.content == b"IMG"
    assert not image.exists()


@pytest.mark.asyncio
async def test_a_file_in_a_shared_task_folder_leases_every_run_that_uses_it(library):
    first, second, other = library.seed("a"), library.seed("b"), library.seed("c")
    path = library.video_in(first, "shared-task")
    library.video_in(second, "shared-task", "recording-2.mp4")
    library.video_in(other, "other-task")

    assert sorted(await asyncio.to_thread(run_media.owners_of_file, path)) == sorted(
        [first, second]
    )
    assert await asyncio.to_thread(run_media.owners_of_file, library.traces.parent / "x.mp4") == []


@pytest.mark.asyncio
async def test_files_outside_any_run_need_no_lease_and_still_download(library, qa, media_roots):
    stray = library.traces / "stray" / "clip.mp4"
    stray.parent.mkdir()
    stray.write_bytes(b"CLIP")

    async with qa:
        response = await qa.get("/videos/stray/clip.mp4")

    assert response.status_code == 200 and response.content == b"CLIP"
