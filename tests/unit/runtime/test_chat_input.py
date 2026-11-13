import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from tests.unit.runtime.test_chat_control import store as store
from artemis.runtime.chat_input import request_input, submit_input


@pytest.fixture(autouse=True)
def socket_root(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="chat-input-", dir="/tmp") as directory:
        monkeypatch.setenv("ARTEMIS_INPUT_SOCKET_DIR", directory)
        yield Path(directory)


async def pending_request(store, identity="run"):
    for attempt in range(1000):
        events = [
            event for event in store.get(identity)["messages"] if event["kind"] == "input_request"
        ]
        if events:
            return events[-1]["payload"]["request_id"]
        await asyncio.sleep(0.001)
    pytest.fail("Input request was not published")


@pytest.mark.asyncio
@pytest.mark.parametrize("sentinel", ["SENTINEL-SECRET-DO-NOT-PERSIST-8593", "🧀" * 4096])
async def test_secret_is_single_use_and_never_reaches_storage_logs_or_traces(
    store, caplog, socket_root, sentinel
):
    store.transition("run", "running")
    sent = []

    async def device_input(value):
        sent.append(value)
        return True

    task = asyncio.create_task(request_input(store, "run", "Enter password", device_input))
    request_id = await pending_request(store)
    try:
        assert await submit_input(store, "run", request_id, sentinel) == {"accepted": True}
        assert await task == "•••• sent to device"
        assert sent == [sentinel]
        with pytest.raises(ValueError, match="input_request_replayed"):
            await submit_input(store, "run", request_id, sentinel)
        with store.transaction() as connection:
            database = "\n".join(connection.iterdump())
        assert sentinel not in database
        assert sentinel not in json.dumps(store.get("run"))
        assert sentinel not in caplog.text
        assert list(socket_root.iterdir()) == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_insecure_socket_directory_fails_closed(store, socket_root):
    socket_root.chmod(0o777)
    with pytest.raises(ValueError, match="input_broker_unavailable"):
        await request_input(store, "run", "Password", None)
    assert not any(event["kind"] == "input_request" for event in store.get("run")["messages"])


@pytest.mark.asyncio
async def test_expired_input_is_rejected_without_a_device_action(store):
    store.transition("run", "running")
    calls = []

    async def device_input(value):
        calls.append(value)
        return True

    task = asyncio.create_task(request_input(store, "run", "Password", device_input, ttl=0.03))
    request_id = await pending_request(store)
    assert await task == "Input request expired. No input was sent to device."
    with pytest.raises(ValueError, match="input_request_expired"):
        await submit_input(store, "run", request_id, "Expired secret")
    assert calls == []


@pytest.mark.asyncio
async def test_wrong_session_and_wrong_request_cannot_consume_input(store):
    store.transition("run", "running")

    async def device_input(value):
        return True

    task = asyncio.create_task(request_input(store, "run", "Password", device_input))
    request_id = await pending_request(store)
    try:
        with pytest.raises(ValueError, match="input_request_wrong_session"):
            await submit_input(store, "missing", request_id, "Secret")
        with pytest.raises(ValueError, match="input_request_not_found"):
            await submit_input(store, "run", "wrong", "Secret")
        assert await submit_input(store, "run", request_id, "Secret") == {"accepted": True}
        assert await task == "•••• sent to device"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_device_failure_does_not_echo_the_secret(store, caplog):
    store.transition("run", "running")
    sentinel = "SECRET-IN-DEVICE-EXCEPTION-9401"

    async def device_input(value):
        raise RuntimeError(f"Device command failed: {value}")

    task = asyncio.create_task(request_input(store, "run", "Password", device_input))
    request_id = await pending_request(store)
    try:
        with pytest.raises(ValueError, match="input_device_failed"):
            await submit_input(store, "run", request_id, sentinel)
        assert await task == "Input could not be confirmed on the device."
        assert sentinel not in caplog.text
        assert sentinel not in json.dumps(store.get("run"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_turn_cannot_receive_pending_secret(store):
    store.transition("run", "running")
    calls = []

    async def device_input(value):
        calls.append(value)
        return True

    task = asyncio.create_task(request_input(store, "run", "Password", device_input))
    request_id = await pending_request(store)
    try:
        store.transition("run", "cancelled")
        with pytest.raises(ValueError, match="input_session_ended"):
            await submit_input(store, "run", request_id, "Secret")
        assert calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_submit_accepts_only_one_value_and_does_not_expire_a_claimed_action(store):
    store.transition("run", "running")
    calls = []

    async def device_input(value):
        calls.append(value)
        await asyncio.sleep(0.15)
        return True

    task = asyncio.create_task(request_input(store, "run", "Password", device_input, ttl=0.1))
    request_id = await pending_request(store)
    results = await asyncio.gather(
        submit_input(store, "run", request_id, "First"),
        submit_input(store, "run", request_id, "Second"),
        return_exceptions=True,
    )
    assert len(calls) == 1
    assert {"accepted": True} in results
    assert any(isinstance(result, ValueError) and "replayed" in str(result) for result in results)
    assert await task == "•••• sent to device"


@pytest.mark.asyncio
async def test_socket_permissions_raw_wrong_session_and_cancellation_cleanup(store, socket_root):
    store.transition("run", "running")
    calls = []

    async def device_input(value):
        calls.append(value)
        return True

    task = asyncio.create_task(request_input(store, "run", "Password", device_input))
    request_id = await pending_request(store)
    path = next(socket_root.iterdir())
    assert socket_root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(
        json.dumps(
            {
                "session_id": "other",
                "turn_id": "run",
                "request_id": request_id,
                "value": "PRIVATE-SENTINEL",
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    assert json.loads(await reader.readline()) == {"error": "input_request_wrong_session"}
    writer.close()
    await writer.wait_closed()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == []
    assert not list(socket_root.iterdir())
    assert store.get("run")["messages"][-1]["payload"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_step_boundary_secret_stays_out_of_database_traces_logs_and_model_context(
    store, tmp_path, monkeypatch, caplog
):
    import base64
    import logging
    from io import BytesIO
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from PIL import Image
    from artemis.drivers.android.adb_driver import AndroidAdbDriver
    from artemis.drivers.base import ScreenData
    from artemis.runtime.chat_control import ChatControl, read_chat_guidance
    from artemis.runtime.chat_input import private_input_active
    from artemis.utils.notes import get_note_file_path

    sentinel = "BOUNDARY-SECRET-SENTINEL-7245"
    device = MagicMock()
    client = MagicMock()
    client.device.return_value = device
    device.shell.return_value = "native-ime"
    device.shell2.return_value = SimpleNamespace(returncode=0, output="")
    driver = AndroidAdbDriver("fake-device", client, MagicMock())
    target = {"resource-id": "app:id/password", "bounds": "[0,0][100,100]"}
    image = Image.new("RGB", (100, 100), "white")
    buffer = BytesIO()
    image.save(buffer, "PNG")

    async def snapshot():
        return ScreenData(
            screenshot_bytes=buffer.getvalue(),
            screenshot_base64=base64.b64encode(buffer.getvalue()).decode(),
            ui_elements=[
                {
                    **target,
                    "class": "android.widget.EditText",
                    "focused": "true",
                    "password": "true",
                    "text": sentinel,
                }
            ],
            ui_hierarchy_xml=f'<hierarchy><node password="true" text="{sentinel}" bounds="[0,0][100,100]"/></hierarchy>',
        )

    driver.get_screen_data = snapshot
    monkeypatch.setattr("artemis.drivers.factory.get_driver", lambda context: driver)
    monkeypatch.setattr("artemis.config.DB_PATH", store.path)
    monkeypatch.setenv("ARTEMIS_CHAT_SESSION_ID", "run")
    context = SimpleNamespace(
        data_engine=SimpleNamespace(base_dir=tmp_path), chat_control=ChatControl(store, "run", None)
    )
    note = get_note_file_path(tmp_path, "input_request")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(json.dumps({"label": "Enter password", "secret": True}))
    boundary = asyncio.create_task(read_chat_guidance(context, "Check login"))
    request_id = await pending_request(store)
    assert private_input_active(driver.device_id)
    assert await submit_input(store, "run", request_id, sentinel) == {"accepted": True}
    guidance = await boundary
    assert "•••• sent to device" in guidance
    assert not note.exists()
    assert context.private_input_used
    assert driver._private_input_used
    assert sum(sentinel in str(call) for call in device.shell2.call_args_list) == 1
    assert not driver._ui_adb_client.set_clipboard.called
    observed = await snapshot()
    model_context = json.dumps(
        {
            "guidance": guidance,
            "history": store.model_history("run"),
            "screen": observed.model_dump(mode="json", exclude={"screenshot_bytes"}),
        }
    )
    trace = tmp_path / "step-trace.json"
    trace.write_text(model_context)
    caplog.set_level(logging.INFO)
    logging.getLogger("artemis.test").info("Step context: %s", model_context)
    with store.transaction() as connection:
        database = "\n".join(connection.iterdump())

    def assert_no_leak(database_text, trace_text, logs, prompt):
        assert sentinel not in database_text + trace_text + logs + prompt

    assert_no_leak(database, trace.read_text(), caplog.text, model_context)
    with pytest.raises(AssertionError):
        assert_no_leak(database, trace.read_text() + sentinel, caplog.text, model_context)
    assert Image.open(BytesIO(observed.screenshot_bytes)).getpixel((50, 50)) == (0, 0, 0)
    store.finish("run", "completed")
    assert private_input_active(driver.device_id)
    assert private_input_active(None)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE chat_messages SET payload=json_set(payload,'$.privacy_until',0) WHERE kind='input_request'"
        )
    assert not private_input_active(driver.device_id)
