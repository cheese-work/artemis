import asyncio
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from artemis.config import DB_PATH
from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime import current_adb_endpoint, pid_is_alive
from apps.admin_console.core.access_control import AdminAPIError
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    present_session_data,
    require_access,
    require_actor,
    require_visible_run,
)
from apps.admin_console.services.task_queue_service import task_queue_service

router = APIRouter(tags=["chat sessions"])


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32768)
    client_id: str = Field(min_length=1, max_length=128)


class ControlRequest(BaseModel):
    client_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)


def _session(identity, scope, *, write=False):
    require_actor(scope)
    store = ChatStore(DB_PATH)
    try:
        session = store.get(identity)
    except KeyError:
        raise AdminAPIError(404, "Chat session not found.", "chat_not_found") from None
    root = session["session_id"]
    if write:
        require_access(scope, root)
    else:
        require_visible_run(require_actor(scope), [root])
    return store, session


@router.get("/api/chats/{session_id}")
async def get_chat(session_id: str, scope: OwnerScope = Depends(actor_scope)):
    store, session = await asyncio.to_thread(_session, session_id, scope)
    return await asyncio.to_thread(present_session_data, scope, session["session_id"], session)


async def _ensure_worker(store, identity, message):
    session = store.get(identity)
    pid = session["lease_pid"]
    if pid and pid_is_alive(pid, session["lease_birth"]):
        return
    turn_id = message["turn_id"]
    if task_queue_service._queue_item_for(turn_id):
        return
    if store.turn(turn_id)["status"] != "queued":
        return
    config = store.launch_config(identity)
    if not config or not config.get("device_serial"):
        store.fail_admission(
            turn_id,
            "The original phone binding is unavailable. Start a new session on a connected phone.",
        )
        raise AdminAPIError(
            409,
            "The original phone binding is unavailable.",
            "session_device_unavailable",
            "Start a new session on a connected phone.",
        )
    if not config.get("host_id") and config.get("adb_endpoint") != current_adb_endpoint().to_dict():
        store.fail_admission(
            turn_id,
            "The session phone endpoint has changed. Reconnect the original phone endpoint or start a new session.",
        )
        raise AdminAPIError(
            409,
            "The session phone endpoint has changed.",
            "session_device_unavailable",
            "Reconnect the original phone endpoint or start a new session.",
        )
    goal = next(item["content"] for item in store.pending(turn_id) if item["role"] == "human")
    options = {
        key: value
        for key, value in config.items()
        if key
        in (
            "profile",
            "expected_output",
            "enable_outputter",
            "verification_level",
            "explorer_mode",
            "locked_app_package",
            "device_serial",
            "host_id",
            "requested_by",
            "bridge_session_id",
            "model",
            "model_explicit",
        )
        and value is not None
    }
    try:
        await task_queue_service.enqueue_tasks(
            [goal], session_id=turn_id, ingress="frontend", **options
        )
    except Exception:
        store.fail_admission(
            turn_id,
            "The session phone could not be re-acquired. Reconnect the original phone, then send another message.",
        )
        raise


@router.post("/api/chats/{session_id}/messages")
async def send_message(
    session_id: str, request: MessageRequest, scope: OwnerScope = Depends(actor_scope)
):
    store, session = await asyncio.to_thread(_session, session_id, scope, write=True)
    try:
        message = await asyncio.to_thread(
            store.send, session["session_id"], request.content, request.client_id
        )
    except ValueError as error:
        raise AdminAPIError(409, str(error), str(error)) from error
    await _ensure_worker(store, session["session_id"], message)
    return message


async def _control(identity, request, scope, kind):
    store, session = await asyncio.to_thread(_session, identity, scope, write=True)
    try:
        return await asyncio.to_thread(
            store.send, session["session_id"], kind.capitalize(), request.client_id, kind=kind
        )
    except ValueError as error:
        raise AdminAPIError(409, str(error), str(error)) from error


@router.post("/api/chats/{session_id}/pause")
async def pause_chat(
    session_id: str, request: ControlRequest, scope: OwnerScope = Depends(actor_scope)
):
    return await _control(session_id, request, scope, "pause")


@router.post("/api/chats/{session_id}/continue")
async def continue_chat(
    session_id: str, request: ControlRequest, scope: OwnerScope = Depends(actor_scope)
):
    return await _control(session_id, request, scope, "continue")
