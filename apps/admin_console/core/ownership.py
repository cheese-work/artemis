"""Run ownership: who may see a run and who may act on it (CHE-1150, slice O1).

A run's owner is the verified Cloudflare identity that submitted it, stored as
``run_meta.requested_by``. A run submitted without an identity has no owner.
Open mode never filters. In cloudflare mode a caller lists their own runs;
``scope=available`` also includes previously opened full-id links. Only owners
and admins may act on a run. An admin may widen a listing with ``scope=all``.
An unowned run is discoverable through a full-id link or ``scope=all``, but actionable by an admin only: a
caller with no identity owns nothing (a missing owner is not a matching one).
Shared session JSON masks secrets for non-owners. Owners, administrators and
open-mode callers retain raw data, as on the catalog's direct run endpoint.

Every handler resolves its actor with ``require_actor``: a missing actor is
refused, and an in-process caller with no request acts as ``SYSTEM_PRINCIPAL``
explicitly (CHE-1385, docs/spaces-contract.md).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from fastapi import Depends, HTTPException, Query, Request

from apps.admin_console.core.access_control import (
    RETRY_AFTER_SECONDS,
    AccessIdentity,
    AdminAPIError,
    public_tier,
)
from artemis.data_engine import run_catalog
from apps.admin_console.core.redaction import redact_image_data, redact_json

try:
    from admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        run_catalog_repo,
    )
except ImportError:
    from apps.admin_console.database.repositories.run_catalog_repository import (
        CatalogNotReady,
        run_catalog_repo,
    )

SCOPE_MINE = "mine"
SCOPE_AVAILABLE = "available"
SCOPE_ALL = "all"


@dataclass(frozen=True)
class OwnerScope:
    enforced: bool
    email: str | None = None
    admin: bool = False
    include_all: bool = False
    # Emails whose runs this caller owns. None: spaces are off and the caller's own email
    # decides. With spaces on, only emails the principal reserved and nobody contests.
    owned_emails: frozenset[str] | None = None
    principal_id: str | None = None
    available: bool = False
    shared_ids: frozenset[str] = frozenset()

    def owner_emails(self) -> list[str]:
        """Every ``requested_by`` value this caller owns, for SQL filters."""
        if self.owned_emails is not None:
            return sorted(self.owned_emails)
        return [self.email] if self.email else []

    def sees(self, owner: str | None, session_id: str | None = None) -> bool:
        """Whether a listing, queue or stream for this scope includes the run."""
        return (
            not self.enforced
            or self.include_all
            or self._owns(owner)
            or (self.available and session_id in self.shared_ids)
        )

    def may_act_on(self, owner: str | None) -> bool:
        """Whether stop, resume, delete or clear may touch the run."""
        return not self.enforced or self.admin or self._owns(owner)

    def _owns(self, owner: str | None) -> bool:
        if owner is None:
            return False
        if self.owned_emails is not None:
            return owner in self.owned_emails
        return owner == self.email


@dataclass(frozen=True)
class SystemPrincipal(OwnerScope):
    """The server acting on its own behalf (workers, migrations, tests); never a request."""

    enforced: bool = False


SYSTEM_PRINCIPAL = SystemPrincipal()


def require_actor(actor: Any) -> OwnerScope:
    """The caller's scope. A missing actor is denied, never treated as unscoped."""
    if isinstance(actor, OwnerScope):
        return actor
    raise AdminAPIError(
        401,
        "No actor was supplied for this action.",
        "actor_required",
        "Call through the API, or pass SYSTEM_PRINCIPAL for server-side work.",
    )


def owner_scope(identity: AccessIdentity, scope: str = SCOPE_MINE) -> OwnerScope:
    if scope not in (SCOPE_MINE, SCOPE_AVAILABLE, SCOPE_ALL):
        raise AdminAPIError(
            400,
            f"Unknown scope '{scope}'.",
            "invalid_scope",
            "Use scope=mine, scope=available, or scope=all as an administrator.",
        )
    result = scope_for(identity, include_all=scope == SCOPE_ALL)
    # With spaces on nobody lists every run, open mode included (docs/spaces-contract.md).
    if result.include_all and (identity.spaces or (result.enforced and not result.admin)):
        raise AdminAPIError(
            403,
            "Listing every user's runs is not available while spaces are on."
            if identity.spaces
            else "Only administrators can list every user's runs.",
            "scope_all_requires_admin",
            "Drop scope=all to list your own runs.",
        )
    if result.enforced and identity.email and scope == SCOPE_AVAILABLE:
        try:
            shared_ids = run_catalog_repo.shared_run_ids(identity.email)
        except CatalogNotReady as exc:
            raise _catalog_not_ready() from exc
        result = replace(result, available=True, shared_ids=shared_ids)
    return result


def run_not_visible() -> AdminAPIError:
    return AdminAPIError(
        404,
        "Run not found.",
        "run_not_visible",
        "Check the run id, or ask the person who shared it.",
    )


@contextmanager
def non_admin_misses_are_hidden(scope: OwnerScope):
    """A legacy resolver's 403/404 tells a non-admin which ids and paths exist: say one thing."""
    scope = require_actor(scope)
    try:
        yield
    except HTTPException:
        if scope.enforced and not scope.admin:
            raise run_not_visible() from None
        raise


def require_signed_in(scope: OwnerScope) -> None:
    """Refuse a caller with no verified identity (cloudflare mode only)."""
    if scope.enforced and scope.email is None:
        raise AdminAPIError(
            401,
            "Sign in through Cloudflare Access before opening a run.",
            "not_signed_in",
            "Open the protected SmartQA URL and sign in, then retry.",
        )


def require_visible_run(
    scope: OwnerScope, session_ids: list[str | None], *, by_link: bool = False
) -> None:
    """Evidence follows the run's visibility rule: any signed-in caller holding the full run id.

    ``session_ids`` are the runs that own the evidence; one live run is enough.
    A removed run, an unknown run and evidence with no owning run are all the same
    ``run_not_visible`` 404, so the answer never says which. Admins see everything.

    ``by_link`` is for evidence named by an image name or a file path, where the URL
    carries no run id: the caller must own a live run that owns it, or have opened one
    by its full id (``run_link_shares``). Guessing a path proves nothing.
    """
    scope = require_actor(scope)
    require_signed_in(scope)
    if not scope.enforced or scope.admin:
        require_catalog_ready()
        return
    for sid in session_ids:
        try:
            if sid:
                run_catalog.validate_session_id(sid)
        except ValueError:
            raise AdminAPIError(
                400, "Invalid run id.", "invalid_session_id", "Use the full run id."
            ) from None
    try:
        live = run_catalog_repo.live_run_ids([sid for sid in session_ids if sid])
    except CatalogNotReady as exc:
        raise _catalog_not_ready() from exc
    if live and by_link:
        try:
            mine = {
                sid
                for sid, owner in run_catalog_repo.owners(sorted(live)).items()
                if scope._owns(owner)
            }
            live = frozenset(mine | (live & run_catalog_repo.shared_run_ids(scope.email)))
        except CatalogNotReady as exc:
            raise _catalog_not_ready() from exc
    if not live:
        raise run_not_visible()


def record_run_read(scope: OwnerScope, session_id: str) -> None:
    if scope.enforced and scope.email:
        run_catalog_repo.record_link_share(scope.email, session_id)


def scope_for(identity: AccessIdentity, *, include_all: bool = False) -> OwnerScope:
    """The request's ownership scope.

    With spaces on, global-admin designation is not data access (docs/spaces-contract.md):
    the scope carries no admin bit, so it never widens a listing or an action.
    """
    return OwnerScope(
        identity.auth_mode != "open",
        identity.email,
        identity.admin and not identity.spaces,
        include_all,
        identity.history_emails,
        identity.principal_id,
    )


async def list_scope(
    scope: str = Query(SCOPE_MINE), identity: AccessIdentity = Depends(public_tier)
) -> OwnerScope:
    return owner_scope(identity, scope)


async def actor_scope(identity: AccessIdentity = Depends(public_tier)) -> OwnerScope:
    return owner_scope(identity)


async def evidence_scope(request: Request, actor: OwnerScope = Depends(actor_scope)):
    """``actor_scope`` for evidence routes: signed in, and a run named in the path is visible.

    A handler that returns (it did not raise) was a successful full-id read, so the
    run joins the caller's ``available`` set; failed reads and prefixes record nothing.
    """
    scope = require_actor(actor)
    session_id = request.path_params.get("session_id")
    if session_id is None:
        require_signed_in(scope)  # media and traces resolve their owning run in the handler
        yield actor
        return
    await asyncio.to_thread(require_visible_run, scope, [session_id])
    yield actor
    await asyncio.to_thread(record_run_read, scope, session_id)


def owners_of(session_ids: list[str]) -> dict[str, str | None]:
    """Owner per known run id; ids with no run record are absent from the result."""
    if not session_ids:
        require_catalog_ready()  # an empty listing is still an answer about the catalog
        return {}
    try:
        return run_catalog_repo.owners(session_ids)
    except CatalogNotReady as exc:
        raise _catalog_not_ready() from exc


def _catalog_not_ready() -> AdminAPIError:
    return AdminAPIError(
        503,
        "The run catalog is not ready, so run ownership cannot be checked.",
        "catalog_not_ready",
        "Retry shortly; restart the console if it persists so the catalog migration can run.",
        RETRY_AFTER_SECONDS,
    )


def require_catalog_ready() -> None:
    """Raise the retryable 503 unless the run catalog is ready.

    Called before any unscoped return (open mode, an admin outside spaces, the
    ``SystemPrincipal``), so unknown readiness never yields raw data.
    """
    try:
        run_catalog_repo.require_ready()
    except CatalogNotReady as exc:
        raise _catalog_not_ready() from exc


def present_session_data(scope: OwnerScope, session_id: str | None, data: Any) -> Any:
    """Keep owner/admin data raw; redact text in shared session JSON otherwise."""
    scope = require_actor(scope)
    if not scope.enforced or scope.admin:
        require_catalog_ready()
        return data
    owner = owners_of([session_id]).get(session_id) if session_id else None
    return data if scope.may_act_on(owner) else redact_json(redact_image_data(data))


def require_access(scope: OwnerScope, session_id: str | None) -> None:
    """Raise 403 unless the caller owns the run or is an admin; no state is touched."""
    require_access_all(scope, {session_id})


def require_access_all(scope: OwnerScope, session_ids: set[str | None]) -> None:
    """Like ``require_access`` for an action that reaches every run in ``session_ids``.

    An empty set, a ``None`` member (a run that cannot be attributed) or a run
    with no recorded owner is admin-only.
    """
    scope = require_actor(scope)
    if not scope.enforced or scope.admin:
        require_catalog_ready()
        return
    ids = sorted(sid for sid in session_ids if sid)
    owners = owners_of(ids)
    if (
        session_ids
        and None not in session_ids
        and all(sid in owners and scope.may_act_on(owners[sid]) for sid in ids)
    ):
        return
    raise AdminAPIError(
        403,
        "Only the run's owner or an administrator can do this.",
        "not_run_owner",
        "Ask the person who started the run, or an administrator.",
    )
