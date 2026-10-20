import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from artemis.config import DB_PATH, TRACES_PATH
from artemis.data_engine.chat_sessions import ChatStore
from artemis.runtime.chat_input import submit_input
from artemis.runtime import current_adb_endpoint, pid_is_alive
from apps.admin_console.core.access_control import AdminAPIError, require_qa
from apps.admin_console.core.ownership import (
    OwnerScope,
    actor_scope,
    present_session_data,
    require_access,
    require_actor,
    require_visible_run,
    record_run_read,
)
from apps.admin_console.services.task_queue_service import task_queue_service
from apps.admin_console.services.deploy_drain import ServerDraining, admission_lock
from apps.admin_console.database.repositories.session_repository import session_repo
from apps.admin_console.database.repositories.step_repository import step_repo
from apps.admin_console.services.media_service import media_service
from apps.admin_console.services.automatic_prompt_tips import read_tip
from apps.admin_console.services.qa_results import result_for_turn

router = APIRouter(tags=["chat sessions"])


def _timeline(session, store=None):
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
        tip = read_tip(latest["turn_id"]) if latest["status"] == "completed" else None
        if (
            tip is None
            and latest["status"] == "completed"
            and store is not None
            and store.launch_config(session["session_id"])
            and 0 <= time.time() - (details.get("end_time") or 0) < 95
        ):
            tip = {"state": "generating", "text": None}
        session["verdict"] = {
            "status": latest["status"],
            "outcome": outcome,
            "report": report,
            "details": details,
            "tip": tip,
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
    if await asyncio.to_thread(_sweep_stuck_turns, store, session):
        session = await asyncio.to_thread(store.get, session["session_id"])
    session = await asyncio.to_thread(_timeline, session, store)
    session = await asyncio.to_thread(_attach_results, scope, session)
    return await asyncio.to_thread(present_session_data, scope, session["session_id"], session)


STUCK_TURN_SECONDS = 120
STUCK_TURN_REASON = "Lượt chạy không bắt đầu được. Gửi lại tin nhắn để chạy lại."


def _sweep_stuck_turns(store, session):
    """Fail queued turns that no worker, queue item or run row will ever pick up."""
    pid = session["lease_pid"]
    if pid and pid_is_alive(pid, session["lease_birth"]):
        return False
    swept = False
    for turn in session["turns"]:
        if turn["status"] == "queued" and not task_queue_service._queue_item_for(turn["turn_id"]):
            swept |= store.fail_admission(
                turn["turn_id"], STUCK_TURN_REASON, stuck_after=STUCK_TURN_SECONDS
            )
    return swept


def _attach_results(scope, session):
    turns = session["turns"]
    count = len(turns)
    results = []
    for index, turn in enumerate(turns, start=1):
        turn_id = turn["turn_id"]
        result = result_for_turn(
            DB_PATH,
            TRACES_PATH,
            turn_id,
            turn["status"],
            index,
            count,
        )
        results.append(result)
        record_run_read(scope, turn_id)
    session["results"] = results
    if isinstance(session.get("verdict"), dict) and results:
        session["verdict"]["result"] = results[-1]
    return session


@router.patch("/api/chats/{session_id}/title", dependencies=[Depends(require_qa)])
async def rename_chat(
    session_id: str, request: TitleRequest, scope: OwnerScope = Depends(actor_scope)
):
    store, session = await asyncio.to_thread(_session, session_id, scope)
    await asyncio.to_thread(store.rename, session["session_id"], request.title)
    updated = await asyncio.to_thread(store.get, session["session_id"])
    updated = await asyncio.to_thread(_timeline, updated, store)
    updated = await asyncio.to_thread(_attach_results, scope, updated)
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
        if isinstance(result, ServerDraining):
            store.fail_admission(turn_id, str(result))
            raise result.http_error() from result
        store.fail_admission(
            turn_id,
            "The session phone could not be re-acquired. Reconnect the original phone, then send another message.",
        )
        raise result
    if result.get("status") == "rejected":
        reason = f"Điện thoại chưa kết nối — kết nối lại rồi gửi lại. ({result.get('error')})"
        store.fail_admission(turn_id, reason)
        raise AdminAPIError(
            409,
            "The session phone is not connected.",
            "session_device_unavailable",
            "Reconnect the phone, then send the message again.",
        )


@router.post("/api/chats/{session_id}/messages", dependencies=[Depends(require_qa)])
async def send_message(
    session_id: str, request: MessageRequest, scope: OwnerScope = Depends(actor_scope)
):
    store, session = await asyncio.to_thread(_session, session_id, scope, write=True)

    def persist():
        with admission_lock:
            task_queue_service.require_admission_open()
            return store.send(session["session_id"], request.content, request.client_id)

    try:
        message = await asyncio.to_thread(persist)
    except ServerDraining as error:
        raise error.http_error() from error
    except ValueError as error:
        raise AdminAPIError(409, str(error), str(error)) from error
    await _ensure_worker(store, session["session_id"], message)
    return message


async def _control(identity, request, scope, kind):
    store, session = await asyncio.to_thread(_session, identity, scope, write=True)

    def persist():
        with admission_lock:
            if kind == "continue":
                task_queue_service.require_admission_open()
            return store.send(
                session["session_id"], kind.capitalize(), request.client_id, kind=kind
            )

    try:
        return await asyncio.to_thread(persist)
    except ServerDraining as error:
        raise error.http_error() from error
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


@router.post("/api/chats/{session_id}/input/{request_id}", dependencies=[Depends(require_qa)])
async def send_input(
    session_id: str, request_id: str, request: Request, scope: OwnerScope = Depends(actor_scope)
):
    store, session = await asyncio.to_thread(_session, session_id, scope, write=True)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            body.clear()
            raise AdminAPIError(
                400, "Input is too long.", "input_value_invalid", "Enter at most 4096 characters."
            )
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        payload = None
    finally:
        body.clear()
    value = payload.pop("value", None) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise AdminAPIError(
            400,
            "Enter a value of at most 4096 characters.",
            "input_value_invalid",
            "Enter the requested value again.",
        )
    try:
        return await submit_input(store, session["session_id"], request_id, value)
    except ValueError as error:
        details = {
            "input_request_expired": "This input request expired. Ask the agent for a new request.",
            "input_request_replayed": "This input request was already used.",
            "input_request_wrong_session": "This input request belongs to another session.",
            "input_request_not_found": "This input request does not belong to this session.",
            "input_session_ended": "This session ended. No input was sent to device.",
            "input_broker_unavailable": "The input request is unavailable. Ask the agent for a new request.",
            "input_device_failed": "Device input failed. No secret was added to chat. Ask for a new request.",
        }
        code = str(error) if str(error) in details else "input_device_failed"
        raise AdminAPIError(
            409, details[code], code, "Ask the agent for a new input request."
        ) from None
    finally:
        value = None
