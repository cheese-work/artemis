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

"""Run catalog API: search and list past runs, resolve one run by id or prefix."""

import asyncio
from datetime import datetime, timezone, UTC
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from apps.admin_console.core.ownership import OwnerScope, list_scope, scope_or_open

try:
    from admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        InvalidCursor,
        run_catalog_repo,
    )
except ImportError:
    from apps.admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        InvalidCursor,
        run_catalog_repo,
    )

router = APIRouter(tags=["runs"])


def _error(status_code: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": error, **extra})


def _parse_time(value: str | None) -> float | None:
    """Epoch seconds or an ISO-8601 date/time (naive values are UTC)."""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


@router.get("/api/runs")
async def list_runs(
    q: str | None = None,
    status: str | None = None,
    device: str | None = None,
    host: str | None = None,
    requester: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    scope: OwnerScope = Depends(list_scope),
):
    """Newest-first page of runs; ``next_cursor`` is null on the last page.

    Callers see their own runs; admins add ``scope=all`` to see everyone's.
    """
    scope = scope_or_open(scope)
    owner_filter = {}
    if scope.enforced and not scope.include_all:
        owner_filter = {"owner": scope.email, "unowned": scope.email is None}
    try:
        bounds = {"since": _parse_time(since), "until": _parse_time(until)}
    except ValueError:
        return _error(400, "invalid_time", detail="since/until: epoch seconds or ISO-8601")
    try:
        page = await asyncio.to_thread(
            run_catalog_repo.list_runs,
            q=q,
            status=status,
            device=device,
            host=host,
            requester=requester,
            cursor=cursor,
            limit=limit,
            **owner_filter,
            **bounds,
        )
    except InvalidCursor:
        return _error(400, "invalid_cursor")
    except CatalogNotReady:
        return _error(503, "catalog_not_ready")
    return {"runs": page.runs, "next_cursor": page.next_cursor, "warnings": page.warnings}


@router.get("/api/runs/{session_id}")
async def get_run(session_id: str):
    """One run by full id or 8-character prefix (409 with candidates if ambiguous)."""
    try:
        found = await asyncio.to_thread(run_catalog_repo.get_run, session_id)
    except ValueError:
        return _error(400, "invalid_session_id")
    except CatalogNotReady:
        return _error(503, "catalog_not_ready")
    if found.run:
        return found.run
    if found.candidates:
        return _error(409, "ambiguous_prefix", candidates=found.candidates)
    if found.removed:
        return _error(410, "removed", **found.removed)
    return _error(404, "not_found")
