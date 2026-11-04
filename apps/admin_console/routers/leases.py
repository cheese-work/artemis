"""Device leases over HTTP (CHE-2099, S3.2): exclusive, expiring use of a phone.

Behind ``ARTEMIS_DEVICE_LEASES`` (off by default). The rules live in
``services/device_leases.py``; this file resolves the phone, names the caller and
shapes the answers.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from apps.admin_console.core.access_control import (
    AccessIdentity,
    AdminAPIError,
    require_admin_or_service_read,
    require_qa_or_service,
)
from apps.admin_console.database.repositories.device_repository import (
    DeviceRepository,
    DeviceStoreNotReady,
)
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.services import device_leases as leases
from artemis.config.device_leases import device_leases_enabled

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/leases", tags=["leases"])

MAX_TTL_SECONDS = 4 * 3600


def _require_enabled() -> None:
    if not device_leases_enabled():
        raise AdminAPIError(
            404,
            "Device leases are turned off on this server.",
            "device_leases_disabled",
            "Ask an administrator to set ARTEMIS_DEVICE_LEASES=enabled.",
        )


def _holder(identity: AccessIdentity) -> str:
    return identity.email or identity.service_principal or "local-admin"


def _pools() -> dict[str, list[str]]:
    raw = os.environ.get("ARTEMIS_LEASE_POOLS", "").strip()
    try:
        pools = json.loads(raw) if raw else {}
        if isinstance(pools, dict) and all(
            isinstance(ids, list) and all(isinstance(i, str) for i in ids) for ids in pools.values()
        ):
            return pools
    except json.JSONDecodeError:
        pass
    logger.error("event=lease_pools_invalid")
    raise AdminAPIError(
        503,
        "The lease pools are misconfigured.",
        "lease_pools_invalid",
        'Set ARTEMIS_LEASE_POOLS to {"pool": ["device_id", ...]}.',
    )


def _restricted() -> frozenset[str]:
    return frozenset(
        i.strip() for i in os.environ.get("ARTEMIS_LEASE_RESTRICTED", "").split(",") if i.strip()
    )


def _candidates(body: LeaseRequest) -> list[str]:
    """Canonical device ids to try, in order. A phone's aliases name the same phone."""
    repo = DeviceRepository(run_catalog_repo.db_path)
    wanted = [body.device_id] if body.device_id else _pools().get(body.pool or "")
    try:
        found = [d for d in (repo.resolve(i) for i in wanted or []) if d is not None]
    except DeviceStoreNotReady as exc:
        raise AdminAPIError(
            503, "Device identity is unavailable.", "device_store_not_ready", "Try again shortly."
        ) from exc
    if found:
        return list(dict.fromkeys(found))
    if body.pool:
        raise AdminAPIError(404, "Unknown pool.", "pool_unknown", "Check the pool name.")
    raise AdminAPIError(404, "Unknown phone.", "device_unknown", "Check the device id.")


class LeaseRequest(BaseModel):
    device_id: str | None = Field(default=None, max_length=128)
    pool: str | None = Field(default=None, max_length=64)
    ttl: int = Field(ge=1, le=MAX_TTL_SECONDS)
    purpose: str = Field(default="", max_length=200)
    restricted: bool = False

    @model_validator(mode="after")
    def _one_target(self) -> LeaseRequest:
        if bool(self.device_id) == bool(self.pool):
            raise ValueError("give exactly one of device_id or pool")
        return self


class RenewRequest(BaseModel):
    ttl: int = Field(ge=1, le=MAX_TTL_SECONDS)


def _view(lease: leases.Lease) -> dict:
    return {
        "lease_id": lease.lease_id,
        "device_id": lease.device_id,
        "holder": lease.holder,
        "purpose": lease.purpose,
        "restricted": lease.restricted,
        "space_epoch": lease.space_epoch,
        "created_at": lease.created_at,
        "expires_at": lease.expires_at,
    }


def _busy(holder: leases.Lease) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "The phone is leased to someone else.",
            "code": "device_busy",
            "fix": "Retry after the holder's lease expires, or ask the holder to release it.",
            # The lease id is the holder's handle; a rival learns who and until when, no more.
            "holder": {
                "holder": holder.holder,
                "device_id": holder.device_id,
                "purpose": holder.purpose,
                "expires_at": holder.expires_at,
            },
        },
    )


@router.get("")
def list_leases(_who: AccessIdentity = Depends(require_admin_or_service_read)) -> dict:
    _require_enabled()
    return {"leases": [_view(lease) for lease in leases.active(run_catalog_repo.db_path)]}


@router.post("", status_code=201)
def create_lease(body: LeaseRequest, identity: AccessIdentity = Depends(require_qa_or_service)):
    _require_enabled()
    candidates = _candidates(body)
    try:
        lease = leases.acquire(
            run_catalog_repo.db_path,
            candidates,
            _holder(identity),
            body.ttl,
            body.purpose,
            restricted=body.restricted,
            always_restricted=_restricted(),
        )
    except leases.DeviceBusy as busy:
        return _busy(busy.holder)
    return _view(lease)


@router.post("/{lease_id}/renew")
def renew_lease(
    lease_id: str, body: RenewRequest, identity: AccessIdentity = Depends(require_qa_or_service)
) -> dict:
    _require_enabled()
    return _view(
        leases.renew(
            run_catalog_repo.db_path, lease_id, _holder(identity), body.ttl, admin=identity.admin
        )
    )


@router.post("/{lease_id}/release")
def release_lease(lease_id: str, identity: AccessIdentity = Depends(require_qa_or_service)) -> dict:
    _require_enabled()
    leases.release(run_catalog_repo.db_path, lease_id, _holder(identity), admin=identity.admin)
    return {"status": "released"}
