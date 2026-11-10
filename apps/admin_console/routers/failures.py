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

"""Admin failure ledger: why runs and steps failed, and whether SmartQA or the prompt is to blame."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from apps.admin_console.core.access_control import (
    AccessIdentity,
    require_admin,
    require_admin_or_service_read,
)
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import failure_ledger, qa_results

router = APIRouter(tags=["failures"])


@router.get("/api/system/failures", dependencies=[Depends(require_admin_or_service_read)])
async def get_failures(days: int = Query(failure_ledger.DEFAULT_DAYS, ge=1, le=365)):
    return await asyncio.to_thread(failure_ledger.view, days)


@router.post("/api/system/failures/collect", dependencies=[Depends(require_admin)])
async def collect_failures():
    """Classify failures not in the ledger yet; the first call backfills existing runs."""
    return await asyncio.to_thread(failure_ledger.collect)


@router.post("/api/system/failures/digest", dependencies=[Depends(require_admin)])
async def send_failure_digest():
    """Send the summary of SmartQA-side causes not reported before."""
    return await asyncio.to_thread(failure_ledger.digest)


class RelabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: Literal[
        "smartqa_infra",
        "smartqa_agent",
        "provider",
        "app_defect",
        "user_prompt",
        "qa_environment",
        "unknown",
    ]
    reason: str = Field(min_length=1, max_length=2000)


@router.get("/api/system/qa-results/gate-a", dependencies=[Depends(require_admin)])
async def get_gate_a():
    try:
        return await asyncio.to_thread(qa_results.gate_a, run_catalog_repo.db_path)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@router.post("/api/system/qa-results/{turn_id}/relabel")
async def relabel_qa_result(
    turn_id: str, payload: RelabelRequest, identity: AccessIdentity = Depends(require_admin)
):
    try:
        return await asyncio.to_thread(
            qa_results.relabel_result,
            run_catalog_repo.db_path,
            turn_id,
            category=payload.category,
            reason=payload.reason,
            actor=identity.subject or identity.email or "loopback",
        )
    except KeyError as error:
        raise HTTPException(404, "qa_result_not_found") from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
