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

from fastapi import APIRouter, Depends, Query

from apps.admin_console.core.access_control import require_admin
from apps.admin_console.services import failure_ledger

router = APIRouter(tags=["failures"], dependencies=[Depends(require_admin)])


@router.get("/api/system/failures")
async def get_failures(days: int = Query(failure_ledger.DEFAULT_DAYS, ge=1, le=365)):
    return await asyncio.to_thread(failure_ledger.view, days)


@router.post("/api/system/failures/collect")
async def collect_failures():
    """Classify failures not in the ledger yet; the first call backfills existing runs."""
    return await asyncio.to_thread(failure_ledger.collect)


@router.post("/api/system/failures/digest")
async def send_failure_digest():
    """Send the summary of SmartQA-side causes not reported before."""
    return await asyncio.to_thread(failure_ledger.digest)
