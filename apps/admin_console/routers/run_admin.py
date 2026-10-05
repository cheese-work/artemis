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

"""Pin, delete, retention and storage endpoints for the run library (services.run_retention)."""

import asyncio

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from apps.admin_console.core.access_control import require_admin, require_qa
from apps.admin_console.routers.run_bundle import library_error
from apps.admin_console.services import run_retention, run_storage
from apps.admin_console.services.run_artifacts import RunLibraryError

router = APIRouter(tags=["runs"])


class RetentionUpdate(BaseModel):
    days: int | None = Field(None, ge=run_retention.MIN_DAYS, le=run_retention.MAX_DAYS)
    enabled: bool | None = None


class ClearRequest(BaseModel):
    confirm_count: int = Field(ge=0)


async def _call(function, *args):
    try:
        return await asyncio.to_thread(function, *args)
    except RunLibraryError as exc:
        return library_error(exc)


@router.post("/api/runs/{session_id}/pin", dependencies=[Depends(require_qa)])
async def pin_run(session_id: str):
    return await _call(run_retention.set_pinned, session_id, True)


@router.post("/api/runs/{session_id}/unpin", dependencies=[Depends(require_qa)])
async def unpin_run(session_id: str):
    return await _call(run_retention.set_pinned, session_id, False)


@router.post("/api/runs/{session_id}/delete", dependencies=[Depends(require_admin)])
async def delete_run(session_id: str):
    return await _call(run_retention.delete_run, session_id)


@router.post("/api/runs/clear", dependencies=[Depends(require_admin)])
async def clear_runs(body: ClearRequest):
    """Delete every unpinned, finished run for everyone; ``confirm_count`` must equal how many."""
    return await _call(run_retention.clear_all, body.confirm_count)


@router.get("/api/system/retention")
async def get_retention():
    return await asyncio.to_thread(run_retention.get_settings)


@router.put("/api/system/retention", dependencies=[Depends(require_admin)])
async def update_retention(body: RetentionUpdate):
    return await _call(lambda: run_retention.update_settings(days=body.days, enabled=body.enabled))


@router.post("/api/system/retention/dry-run", dependencies=[Depends(require_admin)])
async def retention_dry_run(
    days: int | None = Query(None, ge=run_retention.MIN_DAYS, le=run_retention.MAX_DAYS),
):
    """What enforcement would delete now. Deletes nothing; enabling needs this first."""
    return await _call(run_retention.dry_run, days)


@router.post("/api/system/retention/run", dependencies=[Depends(require_admin)])
async def run_retention_now():
    return await _call(run_retention.enforce)


@router.get("/api/system/storage")
async def get_storage():
    return await asyncio.to_thread(run_storage.storage_report)
