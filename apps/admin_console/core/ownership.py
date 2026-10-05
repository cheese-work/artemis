"""Run ownership: who may see a run and who may act on it (CHE-1150, slice O1).

A run's owner is the verified Cloudflare identity that submitted it, stored as
``run_meta.requested_by``. A run submitted without an identity has no owner.
Open mode never filters. In cloudflare mode a caller sees and acts on the runs
they own; an admin may widen a listing with ``scope=all`` and acts on any run.
An unowned run is visible to ``scope=all`` and actionable by an admin, plus by
a caller with no identity (the local CLI/SDK that created it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Query

from apps.admin_console.core.access_control import AccessIdentity, AdminAPIError, public_tier

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
SCOPE_ALL = "all"


@dataclass(frozen=True)
class OwnerScope:
    enforced: bool
    email: str | None = None
    admin: bool = False
    include_all: bool = False

    def sees(self, owner: str | None) -> bool:
        """Whether a listing, queue or stream for this scope includes the run."""
        return not self.enforced or self.include_all or owner == self.email

    def may_act_on(self, owner: str | None) -> bool:
        """Whether stop, resume, delete or clear may touch the run."""
        return not self.enforced or self.admin or owner == self.email


OPEN_SCOPE = OwnerScope(enforced=False)


def scope_or_open(scope: Any) -> OwnerScope:
    """A direct (in-process) call has no dependency injection; it acts unscoped."""
    return scope if isinstance(scope, OwnerScope) else OPEN_SCOPE


def owner_scope(identity: AccessIdentity, scope: str = SCOPE_MINE) -> OwnerScope:
    if scope not in (SCOPE_MINE, SCOPE_ALL):
        raise AdminAPIError(
            400,
            f"Unknown scope '{scope}'.",
            "invalid_scope",
            "Use scope=mine, or scope=all as an administrator.",
        )
    enforced = identity.auth_mode != "open"
    if enforced and scope == SCOPE_ALL and not identity.admin:
        raise AdminAPIError(
            403,
            "Only administrators can list every user's runs.",
            "scope_all_requires_admin",
            "Drop scope=all to list your own runs.",
        )
    return OwnerScope(enforced, identity.email, identity.admin, scope == SCOPE_ALL)


async def list_scope(
    scope: str = Query(SCOPE_MINE), identity: AccessIdentity = Depends(public_tier)
) -> OwnerScope:
    return owner_scope(identity, scope)


async def actor_scope(identity: AccessIdentity = Depends(public_tier)) -> OwnerScope:
    return owner_scope(identity)


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


def require_access(scope: OwnerScope, session_id: str | None) -> None:
    """Raise 403 unless the caller owns the run or is an admin; no state is touched.

    ``session_id`` None (nothing to attribute the action to) counts as unowned.
    """
    if not scope.enforced or scope.admin:
        return
    owner = owners_of([session_id]).get(session_id) if session_id else None
    if scope.may_act_on(owner):
        return
    raise AdminAPIError(
        403,
        "Only the run's owner or an administrator can do this.",
        "not_run_owner",
        "Ask the person who started the run, or an administrator.",
    )
