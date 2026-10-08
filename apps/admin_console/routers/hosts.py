"""Human routes for computers (CHE-1095): read for everyone, change for admins.

Cloudflare Access protects these routes; the machine routes live in agent.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from apps.admin_console.core.access_control import AccessIdentity, AdminAPIError, require_admin
from apps.admin_console.core.ownership import OwnerScope, actor_scope, require_actor
from apps.admin_console.services import host_registry as hr
from apps.admin_console.services.host_hub import CLOSE_REVOKED, host_hub
from apps.admin_console.services.host_registry import host_registry
from apps.admin_console.services.host_tunnel import host_tunnels
from artemis.runtime import device_pool

try:
    from admin_console.core.state import state
    from admin_console.services.bridge_session_service import bridge_session_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.services.bridge_session_service import bridge_session_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/hosts", tags=["hosts"])


def _require_enabled() -> None:
    if not hr.host_agent_enabled():
        raise AdminAPIError(
            404,
            "Computers are turned off on this server.",
            "host_agent_disabled",
            "Ask an administrator to set ARTEMIS_HOST_AGENT=enabled.",
        )


def _active_run_count(host_id: str) -> int:
    return sum(1 for run in state.active_runs.values() if run.get("host_id") == host_id)


@router.get("")
async def list_hosts(actor: OwnerScope = Depends(actor_scope)) -> dict:
    """Computers, and every phone the caller can pick: shared by a computer or plugged into their browser."""
    scope = require_actor(actor)
    if not hr.host_agent_enabled():
        return {"enabled": False, "hosts": [], "devices": []}
    hosts, devices = host_registry.list_hosts()
    for host in hosts:
        host["active_run_count"] = _active_run_count(host["id"])
    # The pool classifies phones from adb properties; a browser phone's address
    # (127.0.0.1:<port>) says nothing about what it is.
    sessions = [s for s in bridge_session_service.live_sessions() if scope.may_act_on(s.owner)]
    pool = {d.serial: d for d in await device_pool.list_devices_async()} if sessions else {}
    browser = [
        {
            "serial": s.serial,
            "model": pool[s.serial].model if s.serial in pool else None,
            "device_kind": pool[s.serial].device_kind if s.serial in pool else "unknown",
            "source": "browser",
            "owner": s.owner,
            "computer_id": None,
            "computer_name": None,
            "computer_status": "online",
            "reason": None,
            "since": None,
        }
        for s in sessions
    ]
    return {"enabled": True, "hosts": hosts, "devices": [*devices, *browser]}


@router.post("/enrollment-codes")
async def create_enrollment_code(identity: AccessIdentity = Depends(require_admin)) -> dict:
    """Mint a one-time code, shown once; it is bound to this admin."""
    _require_enabled()
    return host_registry.create_code(identity.email or "local-admin")


@router.get("/enrollment-codes/{code_id}")
async def enrollment_code_status(code_id: str) -> dict:
    _require_enabled()
    status = host_registry.code_status(code_id)
    if status is None:
        raise AdminAPIError(404, "Unknown code.", "code_invalid", "Create a new code.")
    return status


@router.post("/{host_id}/revoke")
async def revoke_host(host_id: str, _admin: AccessIdentity = Depends(require_admin)) -> dict:
    _require_enabled()
    interrupted = _active_run_count(host_id)
    if not host_registry.revoke(host_id):
        raise AdminAPIError(404, "Unknown computer.", "host_unknown", "Refresh the list.")
    await host_tunnels.abort_host(host_id, "auth_expired")
    await host_hub.close_host(host_id, CLOSE_REVOKED, "revoked")
    logger.info("event=host_revoked host_id=%s interrupted_runs=%d", host_id, interrupted)
    return {"status": "revoked", "interrupted_runs": interrupted}


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.post("/{host_id}/rename")
async def rename_host(
    host_id: str, body: RenameRequest, _admin: AccessIdentity = Depends(require_admin)
) -> dict:
    _require_enabled()
    if not host_registry.rename(host_id, body.name):
        raise AdminAPIError(404, "Unknown computer.", "host_unknown", "Refresh the list.")
    return {"status": "renamed"}
