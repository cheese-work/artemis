import asyncio
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from artemis.config import DB_PATH, TRACES_PATH
from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime import current_adb_endpoint, pid_is_alive
from apps.admin_console.core.access_control import AdminAPIError, require_qa
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    present_session_data,
    require_access,
    require_actor,
    require_visible_run,
)
from apps.admin_console.services.task_queue_service import task_queue_service
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.database.repositories.step_repository import step_repo
from apps.admin_console.services.media_service import media_service

router = APIRouter(tags=["chat sessions"])


def _timeline(session):
    events = [dict(message) for message in session["messages"]]
    recorded = {
        message["payload"].get("step_id"): message
        for message in events
        if message["kind"] == "step"
    }
    for turn in session["turns"]:
        for step in step_repo.get_session_steps(turn["turn_id"]):
            if step["step_id"] in recorded:
                recorded[step["step_id"]]["payload"] = step
                continue
            events.append(
                {
                    "sequence": -step["step_number"],
                    "turn_id": turn["turn_id"],
                    "role": "agent",
                    "kind": "step",
                    "content": step.get("summary") or f"Step {step['step_number']}",
                    "payload": step,
                    "created_at": step.get("timestamp") or turn["created_at"],
                }
            )
    numbers = {turn["turn_id"]: turn["number"] for turn in session["turns"]}
    session["timeline"] = sorted(
        events,
        key=lambda event: (numbers[event["turn_id"]], event["created_at"], event["sequence"]),
    )
    latest = session["turns"][-1]
    if latest["status"] in {"completed", "failed", "interrupted", "cancelled"}:
        details = session_repo.get_session_by_id(latest["turn_id"]) or {}
        outcome = media_service.get_session_checks(latest["turn_id"])["run_outcome"]
        report_path = TRACES_PATH / latest["turn_id"] / "notes" / "output.md"
        try:
            report = report_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            report = None
        session["verdict"] = {
            "status": latest["status"],
            "outcome": outcome,
            "report": report,
            "details": details,
        }
    return session


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32768)
    client_id: str = Field(min_length=1, max_length=128)


class ControlRequest(BaseModel):
    client_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)


class TitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        return value.strip() if isinstance(value, str) else value


def _session(identity, scope, *, write=False):
    require_actor(scope)
    store = ChatStore(DB_PATH)
    try:
        session = store.get(identity)
    except KeyError:
        raise AdminAPIError(
            404, "Chat session not found.", "chat_not_found", "Open an existing session."
        ) from None
    root = session["session_id"]
    if write:
        require_access(scope, root)
    else:
        require_visible_run(require_actor(scope), [root])
    return store, session


@router.get("/api/chats/{session_id}")
async def get_chat(session_id: str, scope: OwnerScope = Depends(actor_scope)):
    store, session = await asyncio.to_thread(_session, session_id, scope)
    session = await asyncio.to_thread(_timeline, session)
    return await asyncio.to_thread(present_session_data, scope, session["session_id"], session)


@router.patch("/api/chats/{session_id}/title", dependencies=[Depends(require_qa)])
async def rename_chat(
    session_id: str, request: TitleRequest, scope: OwnerScope = Depends(actor_scope)
):
    store, session = await asyncio.to_thread(_session, session_id, scope)
    await asyncio.to_thread(store.rename, session["session_id"], request.title)
    updated = await asyncio.to_thread(store.get, session["session_id"])
    updated = await asyncio.to_thread(_timeline, updated)
    return await asyncio.to_thread(present_session_data, scope, session["session_id"], updated)


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
    result = (
        await asyncio.gather(
            task_queue_service.enqueue_tasks(
                [goal], session_id=turn_id, ingress="frontend", **options
            ),
            return_exceptions=True,
        )
    )[0]
    if isinstance(result, asyncio.CancelledError):
        raise result
    if isinstance(result, Exception):
        store.fail_admission(
            turn_id,
            "The session phone could not be re-acquired. Reconnect the original phone, then send another message.",
        )
        raise result


@router.post("/api/chats/{session_id}/messages", dependencies=[Depends(require_qa)])
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


@router.post("/api/chats/{session_id}/pause", dependencies=[Depends(require_qa)])
async def pause_chat(
    session_id: str, request: ControlRequest, scope: OwnerScope = Depends(actor_scope)
):
    return await _control(session_id, request, scope, "pause")


@router.post("/api/chats/{session_id}/continue", dependencies=[Depends(require_qa)])
async def continue_chat(
    session_id: str, request: ControlRequest, scope: OwnerScope = Depends(actor_scope)
):
    return await _control(session_id, request, scope, "continue")
