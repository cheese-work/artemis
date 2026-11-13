"""DLP-1 extensions to CHE-2099. Contract only; no allocating backend installed.

The future authority owner supplies app.state.device_lease_dispatch(op, args).
Never instantiate a broker/database here or fall back to the legacy allocator.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apps.admin_console.core.access_control import (
    AccessIdentity,
    AdminAPIError,
    require_qa_or_service,
)
from artemis.config.device_leases import device_leases_enabled

router = APIRouter(prefix="/dlp-1", tags=["DLP-1 extensions"])
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LocalBinding(Input):
    kind: Literal["local"]
    pid: int = Field(gt=0)
    process_start: Text
    boot_id: Text


class RemoteBinding(Input):
    kind: Literal["remote"]
    # The backend verifies this reference against an authenticated heartbeat.
    heartbeat_id: Text


class Run(Input):
    run_id: Text


class Owner(Input):
    agent_id: Text
    issue_id: Text


class Claim(Run, Owner):
    binding: Annotated[LocalBinding | RemoteBinding, Field(discriminator="kind")]
    idempotency_key: Text


class Acquire(Claim):
    pool: Text = "rig"
    device_id: Text | None = None
    count: int = Field(default=1, ge=1)
    ttl: int = Field(default=1800, ge=300, le=3600)
    purpose: Text


class Renew(Run):
    ttl: int = Field(default=1800, ge=300, le=3600)
    idempotency_key: Text


class Park(Run, Owner):
    pass


class DeviceView(BaseModel):
    device_id: str
    endpoint: str
    restricted: bool
    space_epoch: int | None


class Group(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    lease_id: str
    holder: str
    run_id: str
    purpose: str
    started_at: float
    expires_at: float
    ttl: int
    hard_deadline: float
    state: Literal["active", "fencing", "cleaning", "verifying", "quarantine", "released"]
    recovery_owner: str | None
    ops_issue: str | None
    devices: list[DeviceView] = Field(min_length=1)


class Offer(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    sequence: int = Field(ge=1)
    signal: str
    signaled_at: float | None
    expires_at: float | None
    # Reserved ids only: offers confer no endpoint or credential.
    devices: list[str] = Field(min_length=1)


class Ticket(BaseModel):
    ticket_id: str
    state: Literal["queued", "parked", "offered", "claimed", "cancelled"]
    agent_id: str
    issue_id: str
    position: int | None
    next_offer_sequence: int = Field(ge=1)
    offer: Offer | None


def _unavailable():
    return AdminAPIError(
        503,
        "DLP-1 authority is unavailable.",
        "dlp1_unavailable",
        "Keep the current authority; retry only through the approved broker connection.",
    )


def _call(request, identity, op, args, response_model):
    if not device_leases_enabled():
        raise AdminAPIError(404, "Device leases are turned off.", "device_leases_disabled", "")
    # DLP-1 cutover requires service authentication. Admin privileges never
    # bypass another run's ownership. Existing CHE-2099 auth stays unchanged.
    if not identity.service_principal:
        raise AdminAPIError(403, "A service principal is required.", "service_required", "")
    dispatch = getattr(request.app.state, "device_lease_dispatch", None)
    if dispatch is None:
        raise _unavailable()
    try:
        result = dispatch(op, dict(args, principal=identity.service_principal))
    except (OSError, ValueError, RuntimeError) as exc:
        # A reply can be lost after commit: preserve the client's key for retry.
        raise _unavailable() from exc
    try:
        if not isinstance(result, dict):
            raise ValueError("invalid authority response")
        if "code" not in result:
            return response_model.model_validate(result).model_dump()
        code = result["code"]
        status = {
            "invalid_request": 422,
            "device_unknown": 404,
            "lease_unknown": 404,
            "ticket_unknown": 404,
            "lease_not_yours": 403,
            "ticket_not_yours": 403,
            "lease_expired": 410,
            "ticket_expired": 410,
            "device_busy": 409,
            "impossible_batch": 409,
            "run_already_holds": 409,
            "idempotency_conflict": 409,
            "draining": 409,
            "hard_cap": 409,
            "renew_too_early": 409,
            "conflicting_waiter": 409,
            "lease_not_active": 409,
            "ticket_not_offered": 409,
            "run_not_live": 409,
            "state_conflict": 409,
        }.get(code)
        if status is None:
            raise ValueError("unknown refusal")
        content = {"code": code}
        if code == "device_busy":
            holder = result.get("holder", {})
            # A rival must not receive a lease handle, binding or endpoint.
            content["holder"] = {
                k: holder[k]
                for k in ("holder", "purpose", "expires_at", "device_id")
                if k in holder
            }
            if result.get("ticket") is not None:
                content["ticket"] = Ticket.model_validate(result["ticket"]).model_dump()
        return JSONResponse(status_code=status, content=content)
    except (ValueError, TypeError) as exc:
        raise _unavailable() from exc


@router.post("", status_code=201, response_model=Group)
def acquire(
    body: Acquire, request: Request, identity: AccessIdentity = Depends(require_qa_or_service)
):
    return _call(request, identity, "acquire", body.model_dump(), Group)


@router.get("/tickets/{ticket_id}", response_model=Ticket)
def ticket(
    ticket_id: Text,
    agent_id: Text,
    issue_id: Text,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(
        request,
        identity,
        "ticket",
        dict(ticket_id=ticket_id, agent_id=agent_id, issue_id=issue_id),
        Ticket,
    )


@router.post("/tickets/{ticket_id}/park", response_model=Ticket)
def park(
    ticket_id: Text,
    body: Park,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(request, identity, "park", dict(body.model_dump(), ticket_id=ticket_id), Ticket)


@router.post("/tickets/{ticket_id}/claim", response_model=Group)
def claim(
    ticket_id: Text,
    body: Claim,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(request, identity, "claim", dict(body.model_dump(), ticket_id=ticket_id), Group)


@router.get("/{lease_id}", response_model=Group)
def status(
    lease_id: Text,
    run_id: Text,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(request, identity, "status", dict(lease_id=lease_id, run_id=run_id), Group)


@router.post("/{lease_id}/renew", response_model=Group)
def renew(
    lease_id: Text,
    body: Renew,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(request, identity, "renew", dict(body.model_dump(), lease_id=lease_id), Group)


@router.post("/{lease_id}/release", response_model=Group)
def release(
    lease_id: Text,
    body: Run,
    request: Request,
    identity: AccessIdentity = Depends(require_qa_or_service),
):
    return _call(request, identity, "release", dict(body.model_dump(), lease_id=lease_id), Group)
