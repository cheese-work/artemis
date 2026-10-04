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

"""Deploy drain: close admission to new runs while accepted work finishes."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.admin_console.core.access_control import require_effective_loopback

try:
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.services.task_queue_service import task_queue_service

router = APIRouter(
    prefix="/api/system/drain",
    tags=["system"],
    dependencies=[Depends(require_effective_loopback)],
)


class DrainStatus(BaseModel):
    draining: bool
    active_run_count: int = Field(ge=0)


@router.get("", response_model=DrainStatus)
async def get_drain() -> dict:
    """Report drain state and accepted work that has not finished cleanup."""
    return task_queue_service.drain_status()


@router.post("", response_model=DrainStatus)
async def enable_drain() -> dict:
    """Close admission to new runs. Idempotent; running work continues."""
    task_queue_service.set_draining(True)
    return task_queue_service.drain_status()


@router.delete("", response_model=DrainStatus)
async def clear_drain() -> dict:
    """Reopen admission. Idempotent."""
    task_queue_service.set_draining(False)
    return task_queue_service.drain_status()
