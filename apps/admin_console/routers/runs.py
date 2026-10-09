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
import math
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from apps.admin_console.core.access_control import (
    RETRY_AFTER_SECONDS,
    AccessIdentity,
    AdminAPIError,
    public_tier,
)
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    owner_scope,
    record_run_read,
    require_actor,
    require_signed_in,
    scope_for,
)
from apps.admin_console.core.redaction import redact_image_data, redact_text

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


async def catalog_scope(
    scope: str = Query("mine"), identity: AccessIdentity = Depends(public_tier)
) -> OwnerScope:
    if scope != "everyone":
        return owner_scope(identity, scope)
    if identity.spaces:
        raise AdminAPIError(
            400,
            "Unknown scope 'everyone'.",
            "invalid_scope",
            "Spaces replace scope=everyone; use scope=mine.",
        )
    if identity.auth_mode != "open" and not identity.email:
        raise AdminAPIError(
            403, "Sign in to see everyone's runs.", "team_requires_identity", "Sign in first."
        )
    return scope_for(identity, include_all=True)


def _present(run: dict[str, Any], scope: OwnerScope, *, team: bool = False) -> dict[str, Any]:
    read_only = team or not scope.may_act_on(run.get("requested_by"))
    result = {**run, "read_only": read_only}
    if read_only and isinstance(result.get("prompt"), str):
        result["prompt"] = redact_text(redact_image_data(result["prompt"]))
    return result


def _error(status_code: int, error: str, **extra: Any) -> JSONResponse:
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)} if status_code == 503 else {}
    content = {"error": error, "retryable": True, **extra} if headers else {"error": error, **extra}
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _parse_time(value: str | None) -> float | None:
    """Epoch seconds or an ISO-8601 date/time (naive values are UTC)."""
    if value is None or not value.strip():
        return None
    try:
        result = float(value)
    except ValueError:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        result = (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    if not math.isfinite(result):
        raise ValueError("time must be finite")
    return result


@router.get("/api/runs")
async def list_runs(
    q: str | None = None,
    status: str | None = None,
    review: str | None = None,
    app_build: str | None = None,
    model: list[str] | None = Query(None),
    suite: str | None = None,
    device: str | None = None,
    host: str | None = None,
    requester: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    scope: OwnerScope = Depends(catalog_scope),
    visibility: str = Query("mine", alias="scope"),
):
    """Newest-first page of runs; ``next_cursor`` is null on the last page.

    Callers see their own runs, plus full-id link shares with ``scope=available``;
    ``scope=everyone`` is a redacted, read-only
    catalog. Unowned runs require a full-id link or admin ``scope=all``.
    """
    scope = require_actor(scope)
    team = visibility == "everyone"
    owner_filter = {}
    if team and scope.enforced and not scope.admin:
        owner_filter = {"owned_only": True}
    if scope.enforced and (not scope.include_all or (team and q and q.strip())):
        # An empty list owns nothing but still queries, so an unready catalog answers 503.
        owner_filter = {
            "owners": scope.owner_emails(),
            "shared_with": scope.email if scope.available and not team else None,
        }
    try:
        bounds = {"since": _parse_time(since), "until": _parse_time(until)}
    except ValueError:
        return _error(400, "invalid_time", detail="since/until: epoch seconds or ISO-8601")
    try:
        page = await asyncio.to_thread(
            run_catalog_repo.list_runs,
            q=q,
            status=status,
            review=review,
            app_build=app_build,
            models=model,
            suite=suite,
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
    return {
        "runs": [_present(run, scope, team=team) for run in page.runs],
        "next_cursor": page.next_cursor,
        "warnings": page.warnings,
        "total": page.total,
    }


@router.get("/api/runs/{session_id}")
async def get_run(session_id: str, scope: OwnerScope = Depends(actor_scope)):
    """Full-id share link, or an owner/admin-only prefix (409 for visible candidates)."""
    scope = require_actor(scope)
    require_signed_in(scope)
    try:
        found = await asyncio.to_thread(
            run_catalog_repo.get_run,
            session_id,
            prefix_owners=scope.owner_emails() if scope.enforced and not scope.admin else None,
        )
    except ValueError:
        return _error(400, "invalid_session_id")
    except CatalogNotReady:
        return _error(503, "catalog_not_ready")
    if found.run:
        if session_id == found.run["session_id"]:
            await asyncio.to_thread(record_run_read, scope, session_id)
        return _present(found.run, scope)
    if found.candidates:
        return _error(
            409,
            "ambiguous_prefix",
            candidates=[_present(run, require_actor(scope)) for run in found.candidates],
        )
    if found.removed:
        return _error(410, "removed", **found.removed)
    return _error(404, "not_found", code="run_not_visible")
