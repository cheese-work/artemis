import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import time
import uuid

from artemis.data_engine.chat_sessions import TERMINAL


def _socket_path(turn_id, request_id):
    if not hasattr(os, "getuid") or not hasattr(asyncio, "start_unix_server"):
        raise ValueError("input_broker_unavailable")
    root = Path(os.getenv("ARTEMIS_INPUT_SOCKET_DIR", f"/tmp/artemis-input-{os.getuid()}"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("input_broker_unavailable")
    name = hashlib.sha256(turn_id.encode()).hexdigest()[:16] + "-" + uuid.UUID(request_id).hex
    path = root / (name + ".sock")
    if len(os.fsencode(path)) > 103:
        raise ValueError("input_broker_unavailable")
    return path


def _finish(store, sequence, state):
    with store.transaction() as connection:
        row = connection.execute(
            "SELECT payload FROM chat_messages WHERE sequence=?", (sequence,)
        ).fetchone()
        if row is None:
            return
        payload = json.loads(row["payload"])
        payload["state"] = state
        content = {
            "sent": "•••• sent to device",
            "failed": "Input was not sent to device.",
            "expired": "Input request expired. No input was sent to device.",
            "cancelled": "Input request cancelled. No input was sent to device.",
        }[state]
        connection.execute(
            "UPDATE chat_messages SET payload=?, content=? WHERE sequence=?",
            (json.dumps(payload), content, sequence),
        )


async def request_input(
    store, turn_id, label, device_input, *, secret=True, ttl=180, target=None, device_id=None
):
    root_id = store.get(turn_id)["session_id"]
    request_id = str(uuid.uuid4())
    expires_at = time.time() + ttl
    path = _socket_path(turn_id, request_id)
    if path.exists():
        raise ValueError("input_broker_unavailable")
    completion = asyncio.get_running_loop().create_future()
    handlers = set()
    claimed = False
    sequence = None

    async def receive(reader, writer):
        nonlocal claimed
        task = asyncio.current_task()
        handlers.add(task)
        finished = None
        try:
            body = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            if (
                not isinstance(body, dict)
                or body.get("session_id") != root_id
                or body.get("turn_id") != turn_id
                or body.get("request_id") != request_id
            ):
                raise ValueError("input_request_wrong_session")
            value = body.pop("value", None)
            if not isinstance(value, str) or not 1 <= len(value) <= 4096:
                raise ValueError("input_value_invalid")
            if claimed:
                raise ValueError("input_request_replayed")
            if time.time() >= expires_at:
                raise ValueError("input_request_expired")
            if store.turn(turn_id)["status"] in TERMINAL:
                raise ValueError("input_session_ended")
            claimed = True
            try:
                result = (await asyncio.gather(device_input(value), return_exceptions=True))[0]
                accepted = result is True
            finally:
                value = None
                result = None
            finished = "sent" if accepted else "failed"
            _finish(store, sequence, finished)
            response = {"accepted": True} if accepted else {"error": "input_device_failed"}
        except ValueError as error:
            code = str(error)
            response = {
                "error": code
                if code
                in {
                    "input_request_wrong_session",
                    "input_value_invalid",
                    "input_request_replayed",
                    "input_request_expired",
                    "input_session_ended",
                }
                else "input_request_invalid"
            }
        except (OSError, TimeoutError):
            response = {"error": "input_request_invalid"}
        except (sqlite3.Error, TypeError, KeyError, RuntimeError):
            finished = "failed" if claimed else None
            response = {"error": "input_device_failed"}
        except asyncio.CancelledError:
            writer.close()
            handlers.discard(task)
            raise
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            if finished and not completion.done():
                completion.set_result(finished)
            handlers.discard(task)

    server = await asyncio.start_unix_server(receive, path=path, limit=32768)
    try:
        path.chmod(0o600)
        sequence = store.record_event(
            turn_id,
            "input_request",
            label,
            payload={
                "request_id": request_id,
                "expires_at": expires_at,
                "secret": secret,
                "state": "pending",
                "target": target,
                "device_id": device_id,
            },
            event_key=f"input:{request_id}",
        )
        try:
            state = await asyncio.wait_for(asyncio.shield(completion), ttl)
        except TimeoutError:
            if claimed:
                state = await completion
            else:
                state = "expired"
                _finish(store, sequence, state)
        return (
            "•••• sent to device"
            if state == "sent"
            else "Input request expired. No input was sent to device."
            if state == "expired"
            else "Input was not sent to device."
        )
    except asyncio.CancelledError:
        if sequence is not None:
            _finish(store, sequence, "cancelled")
        raise
    finally:
        server.close()
        await server.wait_closed()
        for handler in list(handlers):
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        path.unlink(missing_ok=True)


async def submit_input(store, identity, request_id, value):
    try:
        session = store.get(identity)
    except KeyError:
        raise ValueError("input_request_wrong_session") from None
    event = next(
        (
            event
            for event in session["messages"]
            if event["kind"] == "input_request" and event["payload"].get("request_id") == request_id
        ),
        None,
    )
    if event is None:
        raise ValueError("input_request_not_found")
    payload = event["payload"]
    if payload["state"] == "expired" or time.time() >= payload["expires_at"]:
        raise ValueError("input_request_expired")
    if payload["state"] != "pending":
        raise ValueError("input_request_replayed")
    if store.turn(event["turn_id"])["status"] in TERMINAL:
        raise ValueError("input_session_ended")
    if not isinstance(value, str) or not 1 <= len(value) <= 4096:
        raise ValueError("input_value_invalid")
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(_socket_path(event["turn_id"], request_id), limit=32768),
            timeout=5,
        )
        writer.write(
            json.dumps(
                {
                    "session_id": session["session_id"],
                    "turn_id": event["turn_id"],
                    "request_id": request_id,
                    "value": value,
                },
                ensure_ascii=False,
            ).encode()
            + b"\n"
        )
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), timeout=60))
        if response.get("accepted") is not True:
            raise ValueError(response.get("error", "input_device_failed"))
        return {"accepted": True}
    except (OSError, TimeoutError):
        raise ValueError("input_broker_unavailable") from None
    finally:
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


INPUT_GUIDE = (
    "For missing device input, focus the intended editable field, then use save_note with key "
    "input_request and JSON content containing label (string) and secret (boolean, default true). "
    "Secret input requires a masked password field. The next step boundary asks the QA and sends "
    "the value directly to that field. Never request secrets as ordinary chat messages, notes, "
    "or model tool arguments. Only a masked receipt returns to the conversation."
)


async def read_input_request(ctx, control):
    from artemis.drivers.factory import get_driver
    from artemis.utils.notes import get_note_file_path

    if not ctx.data_engine or not ctx.data_engine.base_dir:
        return None
    path = get_note_file_path(ctx.data_engine.base_dir, "input_request")
    try:
        content = path.read_text(encoding="utf-8")
        path.unlink()
    except (OSError, UnicodeError):
        return None
    try:
        request = json.loads(content)
        label = request.get("label") if isinstance(request, dict) else None
        secret = request.get("secret", True) if isinstance(request, dict) else None
        if (
            not isinstance(label, str)
            or not 1 <= len(label.strip()) <= 240
            or not isinstance(secret, bool)
        ):
            return "Input request invalid. Supply a short label and a boolean secret flag."
        driver = get_driver(ctx)
        if not callable(getattr(driver, "input_private", None)):
            return "Private device input is unavailable on this driver. No input was requested."
        snapshot = await driver.get_screen_data()
        focused = next(
            (
                element
                for element in snapshot.ui_elements
                if element.get("focused") in (True, "true")
                and (
                    "EditText" in (element.get("class") or "")
                    or element.get("editable") in (True, "true")
                )
            ),
            None,
        )
        if not focused or secret and focused.get("password") not in (True, "true"):
            return "Focus the intended editable field first. Secrets require a masked password field. No input was requested."
        target = {key: focused[key] for key in ("resource-id", "bounds") if focused.get(key)}
        if not target:
            return "The focused field has no stable selector. No input was requested."
        if secret:
            from artemis.controllers.unified_controller import UnifiedMobileController
            from artemis.utils.video import has_active_session

            if has_active_session(driver.device_id):
                recording = await UnifiedMobileController(ctx).stop_video_recording()
                if not recording.success:
                    return "Secure input cannot proceed while recording is unsafe. No input was requested."
            if getattr(driver, "_recording_process", None):
                await driver.stop_video_recording()
                if getattr(driver, "_recording_process", None):
                    return "Secure input cannot proceed while recording is unsafe. No input was requested."
            ctx.private_input_used = True

        async def device_input(value):
            return await driver.input_private(value, target=target, secret=secret)

        return await request_input(
            control.store,
            control.turn_id,
            label.strip(),
            device_input,
            secret=secret,
            target=target,
            device_id=driver.device_id,
        )
    except (ValueError, OSError):
        return "The input request is unavailable. No input was sent to device."


def private_input_active(device_id):
    from artemis.config import DB_PATH

    if not device_id or not Path(DB_PATH).exists():
        return False
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages'"
            ).fetchone():
                return False
            return (
                connection.execute(
                    """SELECT 1 FROM chat_messages m JOIN chat_turns t ON t.turn_id=m.turn_id
                WHERE m.kind='input_request' AND json_extract(m.payload,'$.secret')=1
                AND json_extract(m.payload,'$.device_id')=?
                AND t.status IN ('queued','running','paused') LIMIT 1""",
                    (device_id,),
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return True
