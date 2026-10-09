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

"""``GET /api/runs/{id}/bundle.zip``: one downloadable zip of a run (see services.run_bundle)."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

from apps.admin_console.core.access_control import AccessIdentity, public_tier
from apps.admin_console.core.ownership import (
    OwnerScope,
    owner_scope,
    present_session_data,
    require_actor,
)
from apps.admin_console.services import run_bundle
from apps.admin_console.services.run_artifacts import RunLibraryError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runs"])


def library_error(exc: RunLibraryError, actor: OwnerScope | None = None) -> JSONResponse:
    headers = {}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    content = {"error": exc.code, **exc.extra}
    if isinstance(content.get("candidates"), list):
        content["candidates"] = [
            present_session_data(require_actor(actor), candidate.get("session_id"), candidate)
            for candidate in content["candidates"]
        ]
    return JSONResponse(status_code=exc.status, content=content, headers=headers)


class _BundleResponse(FileResponse):
    """Streams the temp file, then always removes it and releases the run's lease."""

    def __init__(self, bundle: run_bundle.BundleFile):
        super().__init__(
            bundle.path,
            media_type="application/zip",
            filename=f"bundle-{bundle.session_id[:8]}.zip",
        )
        self._bundle = bundle

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Shielded: a client that hangs up must not leave the lease held.
            await asyncio.shield(asyncio.to_thread(self._bundle.finish))


@router.get("/api/runs/{session_id}/bundle.zip")
async def download_bundle(session_id: str, identity: AccessIdentity = Depends(public_tier)):
    """Prompt, steps, images, video and logs. Text is redacted; media is not."""
    try:
        bundle = await asyncio.to_thread(run_bundle.prepare, session_id)
    except RunLibraryError as exc:
        return library_error(exc, owner_scope(identity))
    logger.info(
        "event=bundle_download session_id=%s requester=%s bytes=%d entries=%d skipped=%d ms=%d",
        bundle.session_id,
        identity.email or "local",
        bundle.size,
        bundle.entries,
        bundle.skipped,
        (time.monotonic() - bundle.started) * 1000,
    )
    return _BundleResponse(bundle)
