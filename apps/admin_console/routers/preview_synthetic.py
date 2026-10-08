"""Preview-only control and device routes (CHE-1289).

Included ahead of the real routers when the preview profile is selected, so each
route here answers instead of its real twin (see ``SYNTHETIC`` in
``core.preview_routes``). The only state these handlers change is the in-memory
queue (``state.queue_items``), ``control`` and the private fixture database:
no process, device, provider, lock file or host call. Ownership checks are real.
"""

from dataclasses import dataclass
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.admin_console.core.access_control import AdminAPIError, require_admin, require_qa
from apps.admin_console.core.preview_demo import visible_device_rows
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    list_scope,
    owners_of,
    require_access,
    require_access_all,
    scope_or_open,
)
from apps.admin_console.routers.tasks import _scope_status
from artemis.core.diagnostics.schema import SystemReadinessReport

try:
    from admin_console.core.state import IN_FLIGHT_STATUSES, state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.database.repositories.run_catalog_repository import run_catalog_repo
except ImportError:
    from apps.admin_console.core.state import IN_FLIGHT_STATUSES, state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo

router = APIRouter(tags=["preview"])


@dataclass
class PreviewControl:
    """Preview stand-in for the host's pause marker file."""

    paused: bool = False


control = PreviewControl()


def _queue(*statuses: str) -> list[dict[str, Any]]:
    return [i for i in state.queue_items if isinstance(i, dict) and i.get("status") in statuses]


def _actionable(scope: OwnerScope, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items whose recorded owner the caller may act on; unknown runs never qualify."""
    if not scope.enforced or scope.admin:
        return items
    owners = owners_of(sorted({str(i["session_id"]) for i in items if i.get("session_id")}))
    return [
        i
        for i in items
        if str(i.get("session_id")) in owners and scope.may_act_on(owners[str(i["session_id"])])
    ]


def _cancel_fixture(session_id: str) -> None:
    if not session_repo.update_session_status(session_id, "cancelled"):
        raise HTTPException(status_code=503, detail="The preview outcome could not be persisted.")


@router.get("/api/devices")
async def list_devices(actor: OwnerScope = Depends(actor_scope)) -> dict[str, Any]:
    return {
        "devices": visible_device_rows(scope_or_open(actor))
    }  # [] unless the demo board is seeded


@router.get("/api/stream/device-state")
async def get_device_stream_state() -> dict[str, Any]:
    return {"connected": False, "serial": None, "live_stream_url": None}


@router.get("/api/system/readiness")
async def get_system_readiness() -> dict[str, Any]:
    report = SystemReadinessReport(
        overall_ready=False, blocker_count=0, passed_blocker_count=0, timestamp=0.0
    )
    return report.model_dump(mode="json")


@router.get("/api/status")
async def get_status(scope: OwnerScope = Depends(list_scope)) -> dict[str, Any]:
    active = [
        {
            "device_id": None,
            "session_id": i.get("session_id"),
            "goal": i.get("goal"),
            "pid": None,
            "ingress": None,
            "acquired_at": None,
        }
        for i in _queue(*IN_FLIGHT_STATUSES)
    ]
    latest = session_repo.get_latest_session()
    session_id = (active[0]["session_id"] if active else None) or (
        latest.get("session_id") if latest else None
    )
    payload: dict[str, Any] = {
        "status": ("paused" if control.paused else "running") if active else "idle",
        "session_id": session_id,
        "background_tasks": session_repo.get_background_tasks(session_id) if session_id else [],
        "queue": _queue("pending"),
        "active_tasks": active,
        "model_info": None,
        "ipc_port": None,
    }
    if active:
        payload.update(goal=active[0]["goal"], pid=None, paused_error=None)
    return _scope_status(payload, scope_or_open(scope))


@router.post("/api/stop")
async def stop_task(
    request: Request,
    all: bool = False,
    session_id: str | None = None,
    device_id: str | None = None,
    actor: OwnerScope = Depends(actor_scope),
) -> dict[str, Any]:
    scope = scope_or_open(actor)
    try:
        body = await request.json()
    except ValueError:
        body = None  # empty or non-JSON body: the query parameters decide
    if isinstance(body, dict):
        all = bool(body.get("all")) or all
        session_id = str(body["session_id"]) if body.get("session_id") else session_id
        device_id = str(body["device_id"]) if body.get("device_id") else device_id
    if session_id and device_id:
        raise AdminAPIError(
            400,
            "Name either a session or a device to stop, not both.",
            "ambiguous_stop_target",
            "Send only session_id, or only device_id.",
        )
    if device_id:
        return {"status": "no_running_task"}  # a preview has no devices
    candidates = _queue(*IN_FLIGHT_STATUSES, *(["pending"] if all else []))
    if session_id:
        require_access(scope, session_id)
        targets = [i for i in candidates if str(i.get("session_id")) == session_id]
    else:
        owned = _actionable(scope, candidates)
        targets = owned if all or len(owned) == 1 else []
    for item in targets:
        _cancel_fixture(item["session_id"])
        item["status"] = "stopped"
    if not targets:
        return {"status": "no_running_task"}
    return {"status": "stopped", "session_id": session_id}


@router.post("/api/tasks/{session_id}/cancel-queued")
async def cancel_queued_task(
    session_id: str, actor: OwnerScope = Depends(actor_scope)
) -> dict[str, Any]:
    scope = scope_or_open(actor)
    if scope.enforced and not scope.admin:
        require_access(scope, session_id)
    item = next(
        (
            i
            for i in state.queue_items
            if isinstance(i, dict) and str(i.get("session_id")) == session_id
        ),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    if item["status"] != "pending":
        return {"status": "already_started", "session_id": session_id}
    _cancel_fixture(session_id)
    item["status"] = "cancelled"
    return {"status": "cancelled", "session_id": session_id}


@router.post("/api/resume")
async def resume_task(actor: OwnerScope = Depends(actor_scope)) -> dict[str, str]:
    scope = scope_or_open(actor)
    if scope.enforced and not scope.admin:
        affected = {
            str(i["session_id"]) if i.get("session_id") else None
            for i in _queue(*IN_FLIGHT_STATUSES)
        }
        if affected or control.paused:
            require_access_all(scope, affected)
    if not control.paused:
        return {"status": "not_paused"}
    control.paused = False
    return {"status": "resumed"}


def _delete_fixture(session_id: str, scope: OwnerScope) -> dict[str, str]:
    from artemis.data_engine.storage import StorageManager

    require_access(scope, session_id)
    try:
        uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unknown session.") from exc
    found = run_catalog_repo.get_run(session_id)
    if found.run is None:
        raise HTTPException(status_code=404, detail="Unknown session.")
    if found.run["status"] not in {"completed", "cancelled", "failed", "interrupted"}:
        raise HTTPException(status_code=409, detail="A live preview run cannot be deleted.")
    if found.run["pinned"]:
        raise HTTPException(status_code=409, detail="A pinned preview run cannot be deleted.")
    run_catalog_repo.tombstone(session_id, "manual")
    storage = StorageManager(
        session_repo.db_path or run_catalog_repo.traces_dir / "data_engine.db",
        run_catalog_repo.traces_dir,
    )
    storage.delete_session(uuid.UUID(session_id), delete_files=False, vacuum=False)
    state.queue_items[:] = [
        item for item in state.queue_items if item.get("session_id") != session_id
    ]
    return {"status": "success", "session_id": session_id}


@router.post("/api/sessions/{session_id}/delete", dependencies=[Depends(require_qa)])
async def delete_session(session_id: str, actor: OwnerScope = Depends(actor_scope)):
    return _delete_fixture(session_id, scope_or_open(actor))


@router.post("/api/runs/{session_id}/delete", dependencies=[Depends(require_admin)])
async def delete_run(session_id: str, actor: OwnerScope = Depends(actor_scope)):
    return _delete_fixture(session_id, scope_or_open(actor))
