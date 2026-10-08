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

import asyncio
from pathlib import Path
import re
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from artemis.config import IMAGES_DIR, TRACES_PATH, WORKSPACE_ROOT
from artemis.data_engine.run_catalog import validate_session_id

from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    evidence_scope,
    non_admin_misses_are_hidden,
    present_session_data,
    require_access,
    require_visible_run,
    scope_or_open,
)
from apps.admin_console.services import run_images, run_media
from apps.admin_console.services.run_artifacts import goal_image_session, untracked_inline_image

try:
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services.media_service import media_service

router = APIRouter(tags=["media"])


class _LeasedFileResponse(FileResponse):
    """Streams one file while holding leases on the runs that own it."""

    def __init__(self, path: Path, media_type: str, lease_ids: list[str]):
        super().__init__(path, media_type=media_type)
        self._lease_ids = lease_ids

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Shielded: a client that hangs up must not leave the leases held.
            await asyncio.shield(asyncio.to_thread(run_media.release, self._lease_ids))


async def _leased_file(
    path: Path, media_type: str, owners: list[str], actor: OwnerScope
) -> FileResponse:
    """A download that defers deleting its runs until it ends; 404 once they are all deleted."""
    scope = scope_or_open(actor)
    await asyncio.to_thread(require_visible_run, scope, owners)
    session_id = await asyncio.to_thread(goal_image_session, path)
    if session_id is not None and path.name.startswith("trace_"):
        # Trace-derived image cache: shared trace JSON masks these, so only the owner reads them.
        await asyncio.to_thread(require_access, scope, session_id)
    elif await asyncio.to_thread(untracked_inline_image, path):
        await asyncio.to_thread(require_visible_run, scope, [])  # no capture record: no owner
    lease_ids = await asyncio.to_thread(run_media.lease, owners)
    if lease_ids is None:
        with non_admin_misses_are_hidden(scope):
            raise HTTPException(status_code=404, detail="Media file not found")
    if not lease_ids:
        return FileResponse(path, media_type=media_type)
    return _LeasedFileResponse(path, media_type, lease_ids)


def _safe_session_id(session_id: str) -> str:
    """Ids that name a folder under traces: safe characters, and the real path stays inside."""
    try:
        return validate_session_id(session_id, base_dir=TRACES_PATH)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_session_id")


_VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def _allowed_media_roots() -> list[Path]:
    """Directories the media endpoints are permitted to serve from."""
    roots = []
    for root in (WORKSPACE_ROOT, TRACES_PATH):
        try:
            roots.append(Path(root).resolve())
        except OSError:
            continue
    return roots


def _resolve_media_path(raw_path: str, allowed_suffixes: set[str]) -> Path:
    """Resolve a client-supplied path strictly inside the allowed media roots.

    Every candidate is fully resolved (symlinks, `..`, mixed separators) and
    then re-checked against the allowed roots and an extension allowlist, so a
    crafted URL can never address source code, databases, or dotfiles.
    """
    roots = _allowed_media_roots()
    candidate = Path(raw_path)
    attempts = [candidate] if candidate.is_absolute() else [root / candidate for root in roots]
    for attempt in attempts:
        try:
            resolved = attempt.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        if not any(resolved.is_relative_to(root) for root in roots):
            continue
        if resolved.suffix.lower() not in allowed_suffixes:
            raise HTTPException(status_code=403, detail="File type is not served by the media API.")
        return resolved
    raise HTTPException(status_code=404, detail="Media file not found")


@router.get("/admin", response_class=HTMLResponse)
@router.get("/debug", response_class=HTMLResponse)
async def get_admin_index():
    admin_index = Path(__file__).resolve().parent.parent / "index.html"
    if admin_index.exists():
        return admin_index.read_text(encoding="utf-8")

    admin_index_alt = WORKSPACE_ROOT / "apps" / "admin_console" / "index.html"
    if admin_index_alt.exists():
        return admin_index_alt.read_text(encoding="utf-8")

    return "<h1>Admin Console index.html not found</h1>"


@router.get("/images/{image_name}")
@router.get("/api/images/{image_name}")
async def get_image(image_name: str, actor: OwnerScope = Depends(evidence_scope)):
    with non_admin_misses_are_hidden(actor):
        return await _get_image(image_name, actor)


async def _get_image(image_name: str, actor: OwnerScope):
    inline = re.fullmatch(r"inline_(.+)_([a-f0-9]{64})(?:\.jpg)?", image_name)
    if inline:
        try:
            image_path = run_images.inline_image_path(inline[1], inline[2])
        except ValueError:
            raise HTTPException(status_code=404, detail="Image not found")
        if not image_path.is_file():
            raise HTTPException(status_code=404, detail="Image not found")
        return await _leased_file(image_path.resolve(), "image/jpeg", [inline[1]], actor)
    if not image_name.endswith(".jpg"):
        image_name += ".jpg"
    try:
        images_root = IMAGES_DIR.resolve()
        image_path = (images_root / image_name).resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Image not found")
    if not image_path.is_file() or not image_path.is_relative_to(images_root):
        raise HTTPException(status_code=404, detail="Image not found")

    owners = await asyncio.to_thread(run_media.owners_of_image, image_path.name)
    return await _leased_file(image_path, "image/jpeg", owners, actor)


@router.get("/videos/{video_path:path}")
async def get_video(video_path: str, actor: OwnerScope = Depends(evidence_scope)):
    with non_admin_misses_are_hidden(actor):
        path = _resolve_media_path(video_path, set(_VIDEO_MEDIA_TYPES))
    owners = await asyncio.to_thread(run_media.owners_of_file, path)
    return await _leased_file(path, _VIDEO_MEDIA_TYPES[path.suffix.lower()], owners, actor)


@router.get("/api/sessions/{session_id}/video")
async def get_session_video(session_id: str, actor: OwnerScope = Depends(evidence_scope)):
    # Blocking work (sqlite, filesystem scan, possible ffmpeg conversion) runs
    # off the event loop.
    video = await asyncio.to_thread(_get_session_video_sync, session_id)
    return present_session_data(actor, session_id, video)


def _get_session_video_sync(session_id: str):
    video_rec_map = session_repo.get_video_recordings_map()
    video_idx = media_service.build_video_index()
    row = session_repo.get_session_by_id(session_id)
    row_dict = dict(row) if row else {"session_id": session_id}
    recording = session_repo.get_video_recording_for_session(session_id)
    recording_status = str((recording or {}).get("status") or "")

    if recording_status in ("recording", "finalizing"):
        return {
            "session_id": session_id,
            "status": "processing",
            "has_video": False,
            "video_url": None,
            "video_segments": [],
            "retry_after_ms": 750,
        }

    if recording_status == "failed":
        v_url = media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
        if not v_url:
            return {
                "session_id": session_id,
                "status": "failed",
                "has_video": False,
                "video_url": None,
                "video_segments": [],
                "message": recording.get("error") or "Recording finalization failed",
            }
        # If a video was recovered/found, update DB to ready and proceed to serve it
        session_repo.mark_recording_ready(session_id, v_url)

    v_url = media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
    video_segments = media_service.resolve_video_segments(v_url)
    if v_url:
        version = int(
            float((recording or {}).get("end_time") or row_dict.get("end_time") or 0) * 1000
        )
        separator = "&" if "?" in v_url else "?"
        versioned_url = f"{v_url}{separator}v={version}" if version else v_url
        for segment in video_segments:
            segment_separator = "&" if "?" in segment["url"] else "?"
            segment["url"] = (
                f"{segment['url']}{segment_separator}v={version}" if version else segment["url"]
            )
        return {
            "session_id": session_id,
            "status": "ready",
            "has_video": True,
            "video_url": versioned_url,
            "video_segments": video_segments,
        }

    return {
        "session_id": session_id,
        "status": "unavailable",
        "has_video": False,
        "video_url": None,
        "video_segments": [],
    }


@router.get("/local_file")
async def get_local_file(path: str, actor: OwnerScope = Depends(evidence_scope)):
    with non_admin_misses_are_hidden(actor):
        p, media_type = media_service.get_safe_local_file(path)
    owners = await asyncio.to_thread(run_media.owners_of_file, Path(p))
    return await _leased_file(Path(p), media_type, owners, actor)


@router.get("/api/sessions/{session_id}/plan")
async def get_task_plan(session_id: str, actor: OwnerScope = Depends(evidence_scope)):
    plan = {"plan": media_service.get_task_plan_content(_safe_session_id(session_id), actor)}
    return present_session_data(actor, session_id, plan)


@router.get("/api/sessions/{session_id}/notes")
async def get_all_notes(session_id: str, actor: OwnerScope = Depends(evidence_scope)):
    notes = {"notes": media_service.get_session_notes_content(_safe_session_id(session_id), actor)}
    return present_session_data(actor, session_id, notes)


@router.get("/api/sessions/{session_id}/checks")
async def get_session_checks(session_id: str, actor: OwnerScope = Depends(evidence_scope)):
    """Checker verdict ledger + run outcome (backfill for the Checker panel)."""
    checks = media_service.get_session_checks(_safe_session_id(session_id))
    return present_session_data(actor, session_id, checks)
