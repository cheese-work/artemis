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
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Query, Request

from apps.admin_console.core.access_control import AccessIdentity, AdminAPIError, public_tier
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
    available: bool = False
    shared_ids: frozenset[str] = frozenset()

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
        return owner is not None and owner == self.email


OPEN_SCOPE = OwnerScope(enforced=False)


def scope_or_open(scope: Any) -> OwnerScope:
    """A direct (in-process) call has no dependency injection; it acts unscoped."""
    return scope if isinstance(scope, OwnerScope) else OPEN_SCOPE


def owner_scope(identity: AccessIdentity, scope: str = SCOPE_MINE) -> OwnerScope:
    if scope not in (SCOPE_MINE, SCOPE_AVAILABLE, SCOPE_ALL):
        raise AdminAPIError(
            400,
            f"Unknown scope '{scope}'.",
            "invalid_scope",
            "Use scope=mine, scope=available, or scope=all as an administrator.",
        )
    enforced = identity.auth_mode != "open"
    if enforced and scope == SCOPE_ALL and not identity.admin:
        raise AdminAPIError(
            403,
            "Only administrators can list every user's runs.",
            "scope_all_requires_admin",
            "Drop scope=all to list your own runs.",
        )
    shared_ids = frozenset()
    if enforced and identity.email and scope == SCOPE_AVAILABLE:
        try:
            shared_ids = run_catalog_repo.shared_run_ids(identity.email)
        except CatalogNotReady as exc:
            raise AdminAPIError(
                503, "The run catalog is not ready.", "catalog_not_ready", "Restart the console."
            ) from exc
    return OwnerScope(
        enforced,
        identity.email,
        identity.admin,
        scope == SCOPE_ALL,
        scope == SCOPE_AVAILABLE,
        shared_ids,
    )


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
    scope = scope_or_open(scope)
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
    scope = scope_or_open(scope)
    require_signed_in(scope)
    if not scope.enforced or scope.admin:
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
        raise AdminAPIError(
            503, "The run catalog is not ready.", "catalog_not_ready", "Restart the console."
        ) from exc
    if live and by_link:
        try:
            mine = {
                sid
                for sid, owner in run_catalog_repo.owners(sorted(live)).items()
                if owner == scope.email
            }
            live = frozenset(mine | (live & run_catalog_repo.shared_run_ids(scope.email)))
        except CatalogNotReady as exc:
            raise AdminAPIError(
                503, "The run catalog is not ready.", "catalog_not_ready", "Restart the console."
            ) from exc
    if not live:
        raise run_not_visible()


def record_run_read(scope: OwnerScope, session_id: str) -> None:
    if scope.enforced and scope.email:
        run_catalog_repo.record_link_share(scope.email, session_id)


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
    scope = scope_or_open(actor)
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
        return {}
    try:
        return run_catalog_repo.owners(session_ids)
    except CatalogNotReady as exc:
        raise AdminAPIError(
            503,
            "The run catalog is not ready, so run ownership cannot be checked.",
            "catalog_not_ready",
            "Restart the console so the catalog migration can run.",
        ) from exc


def present_session_data(scope: OwnerScope, session_id: str | None, data: Any) -> Any:
    """Keep owner/admin data raw; redact text in shared session JSON otherwise."""
    scope = scope_or_open(scope)
    if not scope.enforced or scope.admin:
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
    if not scope.enforced or scope.admin:
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
