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

import asyncio
from contextlib import suppress
import json
import logging
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from artemis.core.diagnostics import readiness_engine
from artemis.runtime import DeviceExecutionLock, device_pool
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.config.host_agent import host_agent_enabled
from artemis.runtime.host_endpoints import HostOffline, host_endpoints
from apps.admin_console.services.host_registry import host_registry
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.device_ownership import (
    may_use_device,
    no_device_for,
    own_default_serial,
    require_device,
    visible_devices,
)
from apps.admin_console.services import run_images
from apps.admin_console.services.bridge_session_service import bridge_session_service
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    list_scope,
    owners_of,
    present_session_data,
    record_run_read,
    require_access,
    require_access_all,
    require_actor,
    require_catalog_ready,
    require_visible_run,
)
from apps.admin_console.core.redaction import redact_image_data, redact_json

try:
    from admin_console.core.state import IN_FLIGHT_STATUSES, state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.schemas.task_schema import RunRequest
    from admin_console.services.ipc_service import ipc_service
    from admin_console.services.model_service import model_service
    from admin_console.services.task_preset_catalog import task_recommendation_engine
    from admin_console.services.task_queue_service import ServerDraining, task_queue_service
except ImportError:
    from apps.admin_console.core.state import IN_FLIGHT_STATUSES, state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.schemas.task_schema import RunRequest
    from apps.admin_console.services.ipc_service import ipc_service
    from apps.admin_console.services.model_service import model_service
    from apps.admin_console.services.task_preset_catalog import task_recommendation_engine
    from apps.admin_console.services.task_queue_service import ServerDraining, task_queue_service


router = APIRouter(tags=["tasks"])
logger = logging.getLogger(__name__)

# Lifecycle events every stream historically received; each names a run.
_RUN_BOUND_EVENTS = ("session_started", "session_ended", "background_tasks_updated")
_DROP = object()


def _draining_error(exc: ServerDraining) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": exc.code, "message": str(exc)},
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


async def _bind_bridge_session(request: RunRequest) -> None:
    """Point a run at the phone its bridge holds; a bridge that is gone refuses the run."""
    session = await bridge_session_service.get(request.bridge_session_id)
    if session is None or session.revoked or session.is_expired:
        reason = "missing" if session is None else "revoked" if session.revoked else "expired"
        logger.warning(
            "event=bridge_run_bind_rejected bridge_session_id=%s reason=%s",
            request.bridge_session_id,
            reason,
        )
        raise AdminAPIError(
            409,
            "Your phone is not connected.",
            "bridge_session_unavailable",
            "Connect the phone again, then start the run.",
        )
    named = request.device_serial
    if named and DeviceExecutionLock._normalize_device_id(named) != (
        DeviceExecutionLock._normalize_device_id(session.serial)
    ):
        raise AdminAPIError(
            409,
            "That is not the phone this browser connected.",
            "device_mismatch",
            "Pick the phone shown in the Workspace and start the run again.",
        )
    request.device_serial = session.serial


@router.get("/api/tasks/presets")
async def get_task_presets(
    category: str = "recommended",
    packages: str | None = None,
    limit: int = 24,
):
    """Retrieve smart task recommendations optionally tailored to detected device packages."""
    pkg_list = [p.strip() for p in packages.split(",") if p.strip()] if packages else None
    return task_recommendation_engine.recommend_tasks(
        installed_packages=pkg_list,
        category=category,
        limit=limit,
    )


@router.get("/api/tasks/catalog")
async def get_task_catalog():
    """Retrieve full catalog of predefined tasks and app package registry."""
    return {
        "tasks": [t.model_dump() for t in task_recommendation_engine.get_all_tasks()],
        "app_registry": task_recommendation_engine.get_app_registry(),
    }


@router.post("/api/run")
async def run_task(request: RunRequest, actor: OwnerScope = Depends(actor_scope)):
    scope = require_actor(actor)
    host_id = request.device_ref.host_id if request.device_ref else None
    requested_serial = request.device_ref.serial if request.device_ref else request.device_serial
    if host_id and not host_agent_enabled():
        raise AdminAPIError(
            404, "Computers are turned off.", "host_agent_disabled", "Use a browser phone."
        )
    incoming_goals = []
    if request.goals:
        incoming_goals = request.goals
    elif request.goal:
        incoming_goals = [request.goal]

    if not incoming_goals:
        raise HTTPException(
            status_code=400,
            detail="Either 'goal' or 'goals' list must be provided.",
        )

    # A client-chosen id names folders under traces: refuse anything unsafe up front.
    if request.session_id and not run_images.is_safe_session_id(str(request.session_id)):
        raise AdminAPIError(
            400,
            "The session id is not valid.",
            "invalid_session_id",
            "Leave the session id out, or use letters, digits, '.', '_' and '-' only.",
        )

    # Pictures are checked before anything is probed, stored or queued.
    goal_images = None
    if request.images:
        if len(incoming_goals) != 1:
            raise AdminAPIError(
                400,
                "Images can only be sent with a single goal.",
                "images_need_one_goal",
                "Send one message with its images, or send the goals without images.",
            )
        goal_images = await asyncio.to_thread(run_images.validate, request.images)

    if request.bridge_session_id:
        await _bind_bridge_session(request)
        requested_serial = request.device_serial
    bridge_endpoint = AdbEndpoint.local() if request.bridge_session_id else None
    # A phone the caller does not own is refused before any probe or enqueue.
    if requested_serial and not host_id:
        require_device(scope, requested_serial)

    # Idempotent SDK retries must never re-run device readiness checks. A task
    # can hold the device while its admission response is lost in transit; in
    # that state, probing the same device again may fail or block even though
    # the original task was accepted successfully.
    if request.session_id and len(incoming_goals) == 1:
        requested_sid = str(request.session_id)
        existing_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict) and str(item.get("session_id")) == requested_sid
            ),
            None,
        )
        persisted_session = session_repo.get_session_by_id(requested_sid)
        is_active = (
            str(state.active_session_id) == requested_sid
            or requested_sid in state.active_connections
        )
        if existing_item or persisted_session or is_active:
            require_access(scope, requested_sid)
            existing = dict(existing_item or persisted_session or {})
            device_info = json.loads(existing.get("device_info") or "{}")
            binding = existing.get("device_binding") or device_info.get("device_binding") or {}
            accepted_host = binding.get("host_id", existing.get("host_id"))
            accepted_serial = (
                binding.get("serial")
                or existing.get("device_serial")
                or device_info.get("device_id")
            )
            if accepted_host != host_id or (
                requested_serial and accepted_serial != requested_serial
            ):
                raise AdminAPIError(
                    409,
                    "The run is bound to another device.",
                    "device_ref_conflict",
                    "Use a new session id.",
                )
            # A retry echoes the run's goal and queue item back: owner or admin only.
            task_payload = dict(existing_item or persisted_session or {})
            task_payload.setdefault("session_id", requested_sid)
            task_payload.setdefault("goal", incoming_goals[0])
            task_payload.setdefault("profile", request.profile or "flash")
            task_payload.setdefault("device_serial", requested_serial)
            if binding:
                task_payload.setdefault("device_binding", binding)
                task_payload.setdefault("bridge_session_id", binding.get("bridge_session_id"))
                task_payload.setdefault("host_id", accepted_host)
            task_payload.setdefault("status", "running" if is_active else "queued")
            return {
                "status": task_payload["status"],
                "tasks": [task_payload],
                "enqueued_count": 0,
                "total_queued": len(state.queue_tasks),
            }

    if host_id:
        try:
            host_endpoints.resolve(host_id)
        except HostOffline as error:
            raise AdminAPIError(
                409, "The computer is offline.", "host_offline", "Reconnect the computer."
            ) from error
        _, devices = host_registry.list_hosts()
        if not any(
            device["computer_id"] == host_id and device["serial"] == requested_serial
            for device in devices
        ):
            raise AdminAPIError(
                409,
                "The phone is not shared by this computer.",
                "device_not_shared",
                "Share the phone first.",
            )

    # Accepted retries returned above; anything past this point is new work.
    # Refuse before spending device probes on it. enqueue_tasks re-checks after
    # its own awaits, which is the authoritative admission point.
    try:
        task_queue_service.require_admission_open()
    except ServerDraining as exc:
        raise _draining_error(exc) from exc

    # Reject an explicit unknown/offline target before running the more
    # expensive readiness probe. Besides producing a stable SDK response,
    # this avoids probing the currently active device for a serial that can
    # never be selected. Only a successful, non-empty enumeration may reject:
    # an indeterminate one (adb blip, startup) lets the submission queue and
    # fail downstream with a clear error instead.
    if requested_serial and not host_id:
        try:
            pool = device_pool.pool_for(bridge_endpoint) if bridge_endpoint else device_pool
            rejection = await pool.validate_explicit_serial_async(requested_serial)
        except Exception:
            rejection = None
        if rejection:
            return {
                "status": "rejected",
                "error": rejection,
                "tasks": [],
                "enqueued_count": 0,
                "total_queued": len(state.queue_tasks),
            }

    # Re-check immediately before enqueueing so a device locked between UI
    # polling intervals cannot start through a stale Ready state. Use the
    # bounded submission probe: the full diagnostics path also scans packages,
    # emulator installations, Android version, and screen size.
    # With no explicit serial the probe itself resolves a live target (it
    # prefers the diagnostics target preference, then any unlocked ready
    # device); the verified serial is bound below.
    target_serial = requested_serial or own_default_serial(scope)
    # A scoped caller's auto-selection only ever considers their own and shared devices.
    scoped = scope.enforced and not scope.admin
    device_probe = (
        None
        if host_id
        else await readiness_engine.run_device_submission_probe(
            target_serial=target_serial,
            may_use=(lambda serial: may_use_device(scope, serial)) if scoped else None,
            **({"endpoint": bridge_endpoint} if bridge_endpoint else {}),
        )
    )
    if device_probe and device_probe.summary in {"Device Locked", "Lock State Unknown"}:
        locked_serial = (
            device_probe.metadata.get("active_device", {}).get("serial") or target_serial or ""
        )
        detail = (
            f"Android device {locked_serial} is locked. Unlock it and enter the home screen before running a task.".replace(
                "  ", " "
            ).strip()
            if device_probe.summary == "Device Locked"
            else f"Android device {locked_serial} lock state could not be verified. Keep it unlocked on the home screen and try again.".replace(
                "  ", " "
            ).strip()
        )
        raise HTTPException(status_code=409, detail=detail)

    if device_probe and device_probe.metadata.get("active_device"):
        verified_serial = device_probe.metadata["active_device"].get("serial")
        # Only auto-selected targets may be re-bound to the probed device. An
        # explicitly requested serial is never silently replaced -- if it is
        # invalid, enqueue_tasks rejects the submission with a clear error.
        if verified_serial and not requested_serial:
            require_device(scope, verified_serial)
            target_serial = verified_serial

    # With nothing resolved, the queue would pick any attached phone, someone else's included.
    if scoped and not target_serial:
        raise no_device_for()

    try:
        return await task_queue_service.enqueue_tasks(
            incoming_goals,
            profile=request.profile or "flash",
            expected_output=request.expected_output,
            enable_outputter=request.enable_outputter,
            verification_level=request.verification_level,
            explorer_mode=request.explorer_mode,
            locked_app_package=request.locked_app_package,
            app_path=request.app_path,
            device_serial=target_serial,
            ingress=request.ingress or "frontend",
            session_id=request.session_id,
            conversation_id=request.conversation_id,
            run_id=request.run_id,
            requested_by=scope.email,
            goal_images=goal_images,
            bridge_session_id=request.bridge_session_id,
            **({"host_id": host_id} if host_id else {}),
        )
    except ServerDraining as exc:
        raise _draining_error(exc) from exc


@router.get("/api/run/defaults")
async def get_run_defaults():
    """Effective Pro-profile tuning defaults from the agent config.

    The launcher's sliders start here so they reflect ``artemis.jsonc`` (and the
    ``ARTEMIS_EXPLORER_VERSION`` override) instead of a hard-coded guess.
    """
    from artemis.config import load_agent_config, verification_level_for_checker

    agent_cfg = load_agent_config()
    return {
        "verification_level": verification_level_for_checker(agent_cfg.checker),
        "explorer_mode": agent_cfg.explorer.resolve(profile="pro"),
    }


@router.get("/api/devices")
async def list_devices(actor: OwnerScope = Depends(actor_scope)):
    """List all connected Android devices with their busy / idle status."""
    devices = await device_pool.list_devices_async()
    return {"devices": visible_devices(require_actor(actor), [d.to_dict() for d in devices])}


def _owned_ids(scope: OwnerScope, ids: set[str | None]) -> list[str]:
    """The ids whose recorded owner the caller may act on (unknown runs never qualify)."""
    named = sorted(sid for sid in ids if sid)
    owners = owners_of(named)
    return [sid for sid in named if sid in owners and scope.may_act_on(owners[sid])]


def _require_pause_authority(scope: OwnerScope) -> None:
    """403 unless the caller may resume every run the global pause marker affects.

    The marker is shared: clearing it resumes every running run, in this process
    or any other. Mixed-owner, unowned or unattributable sets are admin-only; with
    nothing running and nothing paused there is nothing to authorize.
    """
    if scope.enforced and not scope.admin:
        affected = task_queue_service.active_session_ids(running_only=True)
        if affected or state.is_paused:
            require_access_all(scope, affected)


def _may_clear_pause(scope: OwnerScope) -> bool:
    try:
        _require_pause_authority(scope)
    except AdminAPIError:
        return False
    return True


def _stop_one(
    session_id: str | None, device_id: str | None, clear_pause: bool = False
) -> dict[str, Any]:
    # stop_tasks updates scheduler state and asyncio events owned by this loop.
    if task_queue_service.stop_tasks(
        clear_all=False, session_id=session_id, device_id=device_id, clear_pause=clear_pause
    ):
        return {"status": "stopped", "session_id": session_id}
    return {"status": "no_running_task"}


def _stop_for_non_admin(
    scope: OwnerScope, clear_all: bool, session_id: str | None, device_id: str | None
) -> dict[str, Any]:
    """Stop only runs the caller owns; any other target is a 403 with no side effect."""
    if session_id and device_id:
        # The stop resolver falls back to the device when the session holds no
        # lock, so an authorized session could reach a foreign run on that device.
        raise AdminAPIError(
            400,
            "Name either a session or a device to stop, not both.",
            "ambiguous_stop_target",
            "Send only session_id, or only device_id.",
        )
    # Decided before anything is stopped. A stop may cancel the caller's own run
    # without resuming everyone else's: the shared pause marker is cleared only
    # if the caller could also have resumed it (same rule as /api/resume).
    clear_pause = _may_clear_pause(scope)
    if session_id:
        require_access(scope, session_id)
        return _stop_one(session_id, None, clear_pause)
    if device_id:
        # Resolve the device to its runs with the stop resolver itself, authorize
        # those, then stop them by session so the device is never re-resolved.
        on_device = task_queue_service.sessions_on_device(device_id)
        if not on_device:
            return {"status": "no_running_task"}
        require_access_all(scope, on_device)
        stopped = [
            sid
            for sid in sorted(on_device, key=str)
            if _stop_one(sid, None, clear_pause)["status"] == "stopped"
        ]
        return (
            {"status": "stopped", "session_id": None} if stopped else {"status": "no_running_task"}
        )
    # "Clear" and the untargeted legacy stop reach only the caller's own runs; the
    # legacy stop keeps its rule of acting only when the target is unambiguous.
    own = _owned_ids(scope, task_queue_service.active_session_ids(running_only=not clear_all))
    if not clear_all and len(own) != 1:
        return {"status": "no_running_task"}
    stopped = [sid for sid in own if _stop_one(sid, None, clear_pause)["status"] == "stopped"]
    return {"status": "stopped", "session_id": None} if stopped else {"status": "no_running_task"}


@router.post("/api/stop")
async def stop_task(
    request: Request,
    all: bool = False,
    session_id: str | None = None,
    device_id: str | None = None,
    actor: OwnerScope = Depends(actor_scope),
):
    scope = require_actor(actor)
    target_all = all
    target_sid = session_id
    target_dev = device_id

    try:
        body = await request.json()
        if isinstance(body, dict):
            if "all" in body:
                target_all = bool(body["all"]) or target_all
            if body.get("session_id"):
                target_sid = str(body["session_id"])
            if body.get("device_id"):
                target_dev = str(body["device_id"])
    except ValueError:
        # Empty or non-JSON body: fall back to the query parameters.
        pass

    if scope.enforced and not scope.admin:
        return _stop_for_non_admin(scope, target_all, target_sid, target_dev)

    # stop_tasks updates scheduler state and asyncio events owned by this loop.
    stopped = task_queue_service.stop_tasks(
        clear_all=target_all,
        session_id=target_sid,
        device_id=target_dev,
    )
    if stopped:
        return {"status": "stopped", "session_id": target_sid}
    return {"status": "no_running_task"}


@router.post("/api/tasks/{session_id}/cancel-queued")
async def cancel_queued_task(session_id: str, actor: OwnerScope = Depends(actor_scope)):
    """Cancel a run only while it waits; a started run is left running.

    Running runs are stopped with ``/api/stop``, never through this route. Like
    stop, it needs the run's owner or an admin; a denied call has no side effect.
    """
    scope = require_actor(actor)
    if scope.enforced and not scope.admin:
        require_access(scope, session_id)
    result = task_queue_service.cancel_queued(session_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="Unknown session.")
    if result == "retry":
        raise HTTPException(
            status_code=503,
            detail="The cancellation could not be saved; retry.",
            headers={"Retry-After": "1"},
        )
    return {"status": result, "session_id": session_id}


@router.post("/api/resume")
async def resume_task(actor: OwnerScope = Depends(actor_scope)):
    """Resume the paused worker.

    The pause marker is global, so a non-admin may clear it only when every run it
    affects (the running ones) is theirs; mixed-owner or unattributable pauses
    are admin-only. With nothing running and nothing paused the call is a no-op.
    """
    _require_pause_authority(require_actor(actor))
    resumed = task_queue_service.resume_task()
    if resumed:
        return {"status": "resumed"}
    return {"status": "not_paused"}


@router.get("/api/status")
async def get_status(scope: OwnerScope = Depends(list_scope)):
    return _scope_status(await _status_payload(), require_actor(scope))


def _scope_status(payload: dict[str, Any], scope: OwnerScope) -> dict[str, Any]:
    """Filter visible runs and redact shared payloads while keeping owner data raw."""
    if not scope.enforced or scope.include_all:
        require_catalog_ready()  # readiness before any unscoped return
        return payload
    queue = list(payload.get("queue") or [])
    active = list(payload.get("active_tasks") or [])
    headline = payload.get("session_id")
    ids = {str(i["session_id"]) for i in [*queue, *active] if i.get("session_id")}
    if headline:
        ids.add(str(headline))
    owners = owners_of(sorted(ids))

    def visible(session_id: Any) -> bool:
        return (
            bool(session_id)
            and str(session_id) in owners
            and scope.sees(owners[str(session_id)], str(session_id))
        )

    def present(session_id: Any, data: Any) -> Any:
        return (
            data
            if scope.may_act_on(owners.get(str(session_id)))
            else redact_json(redact_image_data(data))
        )

    scoped = {
        **(present(headline, payload) if visible(headline) else payload),
        "queue": [
            present(item.get("session_id"), item)
            for item in queue
            if visible(item.get("session_id"))
        ],
        "active_tasks": [
            present(item.get("session_id"), item)
            for item in active
            if visible(item.get("session_id"))
        ],
        "background_tasks": [],
    }
    if not headline:
        return scoped
    if visible(headline):
        # Bind to the run the caller can see, not the globally latest session.
        scoped["background_tasks"] = present(
            headline, session_repo.get_background_tasks(str(headline))
        )
    else:
        scoped.update(session_id=None, goal=None, pid=None)
    return scoped


async def _status_payload() -> dict[str, Any]:
    # Watchdog check to ensure background worker is alive
    task_queue_service.ensure_worker_running()

    latest_session = session_repo.get_latest_session()
    latest_session_id = latest_session.get("session_id") if latest_session else None
    bg_tasks = session_repo.get_background_tasks(latest_session_id) if latest_session_id else []

    global_owner = DeviceExecutionLock.get_active_owner()
    is_running = state.is_running or global_owner is not None
    running_task = next(
        (
            t
            for t in state.queue_items
            if isinstance(t, dict) and t.get("status") in IN_FLIGHT_STATUSES
        ),
        None,
    )
    if not running_task and is_running:
        running_task = next(
            (t for t in state.queue_items if isinstance(t, dict) and t.get("status") == "pending"),
            None,
        )

    active_owners = DeviceExecutionLock.get_active_owners()
    active_tasks = [
        {
            "device_id": owner.device_id,
            "session_id": owner.session_id,
            "goal": owner.description,
            "pid": owner.pid,
            "ingress": owner.ingress,
            "acquired_at": owner.acquired_at,
        }
        for owner in active_owners.values()
    ]

    running_sid = (
        (global_owner.session_id if global_owner else None)
        or state.active_session_id
        or (running_task.get("session_id") if running_task else None)
        or (
            active_tasks[0]["session_id"]
            if active_tasks and active_tasks[0].get("session_id")
            else None
        )
        or (session_repo.get_running_session_id() if is_running else None)
    )
    owner_connection = state.active_connections.get(str(running_sid), {}) if running_sid else {}
    running_goal = (
        owner_connection.get("goal")
        or (global_owner.description if global_owner else None)
        or state.current_goal
        or (running_task.get("goal") if running_task else None)
        or (active_tasks[0]["goal"] if active_tasks else None)
    )

    active_profile = (
        owner_connection.get("profile")
        or state.current_profile
        or (running_task.get("profile") if running_task and not global_owner else None)
    )
    if not active_profile and (running_sid or latest_session_id):
        check_sid = running_sid or latest_session_id
        sess_row = session_repo.get_session_by_id(check_sid)
        if sess_row:
            llm_traces = session_repo.get_llm_traces_for_profile(check_sid)
            agent_names = session_repo.get_agent_trace_names(check_sid)
            active_profile = model_service.resolve_session_profile(
                sess_row, llm_traces, agent_names=agent_names
            )

    model_info = model_service.get_active_model_info(active_profile)

    # Unified Global Queue: merge web tasks and external SDK/CLI device queue tickets
    global_queued = DeviceExecutionLock.get_queued_tasks()
    seen_ids = set()
    queue_data: list[dict[str, Any]] = []

    for item in state.queue_tasks:
        sid = item.get("session_id")
        if sid:
            seen_ids.add(str(sid))
        queue_data.append(item)

    for g_item in global_queued:
        sid = str(g_item.get("session_id"))
        if sid not in seen_ids:
            seen_ids.add(sid)
            queue_data.append(g_item)

    if is_running:
        is_paused = state.is_paused
        return {
            "status": "paused" if is_paused else "running",
            "paused_error": state.paused_error if is_paused else None,
            "goal": running_goal,
            "pid": (
                global_owner.pid
                if global_owner
                else state.current_process.pid
                if state.current_process
                else None
            ),
            "session_id": running_sid,
            "background_tasks": bg_tasks,
            "queue": queue_data,
            "model_info": model_info,
            "active_tasks": active_tasks,
        }

    if latest_session_id and str(latest_session_id) in state.active_connections:
        conn_info = state.active_connections[str(latest_session_id)]
        is_paused = state.is_paused
        conn_profile = conn_info.get("profile") or active_profile
        conn_model_info = model_service.get_active_model_info(conn_profile)
        return {
            "status": "paused" if is_paused else "running",
            "paused_error": state.paused_error if is_paused else None,
            "goal": conn_info.get("goal"),
            "pid": conn_info.get("pid"),
            "session_id": latest_session_id,
            "background_tasks": bg_tasks,
            "queue": queue_data,
            "model_info": conn_model_info,
            "active_tasks": active_tasks,
            "ipc_port": state.ipc_port,
        }

    return {
        "status": "idle",
        "session_id": latest_session_id,
        "background_tasks": bg_tasks,
        "queue": queue_data,
        "active_tasks": active_tasks,
        "model_info": model_info,
        "ipc_port": state.ipc_port,
    }


@router.get("/api/stream")
@router.get("/api/stream/{session_id}")
async def stream_events(
    session_id: str = "active",
    client: str | None = None,
    scope: OwnerScope = Depends(list_scope),
):
    # The "all"/"active" firehose is scoped to the caller's runs. A stream of one
    # named run is a get-by-id (share link): in cloudflare mode it carries that
    # run's events only, never another run's lifecycle events.
    scope = require_actor(scope)
    # Every scope: a scoped stream with no active run never looks up an owner, so an
    # unready catalog would otherwise open it and later events would fail mid-stream.
    require_catalog_ready()
    firehose = session_id in ("all", "active")
    if not firehose:
        await asyncio.to_thread(require_visible_run, scope, [session_id])
        await asyncio.to_thread(record_run_read, scope, session_id)
    decided: dict[str, bool] = {}

    def may_see(event_session_id: Any) -> bool:
        if not event_session_id:
            return True  # system event, not tied to a run
        key = str(event_session_id)
        if key in decided:
            return decided[key]
        owners = owners_of([key])
        allowed = scope.sees(owners.get(key), key)
        if key in owners:  # a run not yet recorded may still gain its owner
            decided[key] = allowed
        return allowed

    def row_allowed(row_session_id: Any) -> bool:
        if not row_session_id:
            return False  # an unattributable row is never shared
        if firehose:
            return scope.include_all or may_see(row_session_id)
        return str(row_session_id) == session_id

    def scoped_payload(event_type: str, data: Any) -> Any:
        """The payload this subscriber may receive, or ``_DROP``."""
        if not scope.enforced or (firehose and scope.include_all):
            return data
        if isinstance(data, list):  # e.g. background_tasks_updated: one row per task
            rows = [r for r in data if isinstance(r, dict) and row_allowed(r.get("session_id"))]
            return [
                present_session_data(scope, row.get("session_id"), row) for row in rows
            ] or _DROP
        if isinstance(data, dict):
            event_session_id = data.get("session_id")
            if firehose:
                return (
                    present_session_data(scope, event_session_id, data)
                    if may_see(event_session_id)
                    else _DROP
                )
            if event_type in _RUN_BOUND_EVENTS and str(event_session_id) != session_id:
                return _DROP
            return present_session_data(scope, session_id, data)
        return _DROP if firehose else present_session_data(scope, session_id, data)

    async def event_generator():
        queue = asyncio.Queue()
        event_loop = asyncio.get_running_loop()

        def callback(event_type, data):
            try:
                data = scoped_payload(event_type, data)
                if data is _DROP:
                    return
                # Global queue lifecycle events should always be delivered
                if event_type not in _RUN_BOUND_EVENTS:
                    # Filter events by session_id when subscribed to a specific session
                    if session_id and session_id not in ("all", "active"):
                        evt_session_id = None
                        if isinstance(data, dict):
                            evt_session_id = data.get("session_id")

                        if evt_session_id and str(evt_session_id) != str(session_id):
                            return

                        if (
                            not evt_session_id
                            and state.active_session_id
                            and str(state.active_session_id) != str(session_id)
                        ):
                            return

                sanitized_data = ipc_service.sanitize_event_data(event_type, data)
                event_loop.call_soon_threadsafe(queue.put_nowait, (event_type, sanitized_data))
            except RuntimeError:
                pass

        state.add_subscriber(callback)
        yield f'event: info\ndata: {{"message": "Subscribed to session {session_id}"}}\n\n'

        if session_id in ("all", "active"):
            active_sid = state.active_session_id
            if not active_sid:
                running_item = next(
                    (
                        t
                        for t in state.queue_items
                        if isinstance(t, dict) and t.get("status") in IN_FLIGHT_STATUSES
                    ),
                    None,
                )
                if running_item:
                    active_sid = running_item.get("session_id")
            if not active_sid:
                owner = DeviceExecutionLock.get_active_owner()
                if owner and owner.session_id:
                    active_sid = owner.session_id

            if active_sid and scope.enforced and not scope.include_all and not may_see(active_sid):
                active_sid = None
            if active_sid:
                goal = state.current_goal or ""
                profile = state.current_profile or "flash"
                session_data = present_session_data(
                    scope,
                    str(active_sid),
                    {"session_id": str(active_sid), "initial_goal": goal, "profile": profile},
                )
                yield (f"event: session_started\ndata: {json.dumps(session_data, default=str)}\n\n")
                for progress_event in state.get_startup_progress(str(active_sid)):
                    progress_event = present_session_data(scope, str(active_sid), progress_event)
                    yield (
                        "event: startup_progress\n"
                        f"data: {json.dumps(progress_event, default=str)}\n\n"
                    )
                try:
                    from apps.admin_console.database.repositories.step_repository import step_repo

                    recorded_steps = step_repo.get_session_steps(str(active_sid))
                    for step_dict in recorded_steps:
                        step_dict = present_session_data(scope, str(active_sid), step_dict)
                        yield (
                            f"event: step_recorded\ndata: {json.dumps(step_dict, default=str)}\n\n"
                        )
                except Exception as exc:
                    print(f"[Stream] Could not replay active steps: {exc}")
        else:
            for progress_event in state.get_startup_progress(session_id):
                progress_event = present_session_data(scope, session_id, progress_event)
                yield (
                    f"event: startup_progress\ndata: {json.dumps(progress_event, default=str)}\n\n"
                )

        shutdown_waiter = asyncio.create_task(state.shutdown_event.wait())
        queue_waiter = None
        try:
            while not state.is_shutting_down:
                queue_waiter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {queue_waiter, shutdown_waiter},
                    timeout=5.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown_waiter in done:
                    queue_waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_waiter
                    queue_waiter = None
                    break
                if queue_waiter in done:
                    event_type, data = queue_waiter.result()
                    queue_waiter = None
                    yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
                else:
                    queue_waiter.cancel()
                    with suppress(asyncio.CancelledError):
                        await queue_waiter
                    queue_waiter = None
                    yield "event: keep-alive\ndata: {}\n\n"
        except asyncio.CancelledError:
            raise
        finally:
            waiters = (queue_waiter, shutdown_waiter)
            for waiter in waiters:
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            for waiter in waiters:
                if waiter is not None:
                    with suppress(asyncio.CancelledError):
                        await waiter
            state.remove_subscriber(callback)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
