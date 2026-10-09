import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch


def test_fresh_server_connect_then_run_uses_the_same_bridge_lease(tmp_path):
    environment = {
        **os.environ,
        "ARTEMIS_APP_DIR": str(tmp_path),
        "ARTEMIS_AUTH_MODE": "open",
        "ARTEMIS_SPACES_ENABLED": "false",
    }
    result = subprocess.run(
        [sys.executable, __file__, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"accepted": true' in result.stdout


def connect_then_run(root):
    from fastapi.testclient import TestClient

    from apps.admin_console import server
    from artemis.data_engine.storage import StorageManager
    from artemis.runtime import AdbEndpoint, DeviceExecutionLock, trace_store

    bridge = server.device_bridge.bridge_session_service
    bridge_module = importlib.import_module(type(bridge).__module__)
    queue = server.task_queue_service
    queue_module = importlib.import_module(queue.__module__)
    calls = []

    async def fake_adb(*arguments):
        calls.append(arguments)
        return f"connected to {arguments[1]}" if arguments[0] == "connect" else "disconnected"

    database = root / "sessions.db"
    StorageManager(database, root)
    queue_module.session_repo.db_path = database
    trace_store.TRACES_DIR = str(root / "traces")
    queue_module.state.queue_items.clear()
    queue_module.state.draining = False
    client = TestClient(server.proxy_aware_app, client=("127.0.0.1", 50000))
    validation = AsyncMock(return_value=None)
    probe = AsyncMock(return_value=None)
    with (
        patch.object(bridge_module, "_run_adb_command", fake_adb),
        patch.object(type(queue), "ensure_worker_running", MagicMock()),
        patch.object(type(queue), "_reject_unavailable_device", validation),
        patch.object(
            server.tasks.device_pool,
            "pool_for",
            return_value=MagicMock(validate_explicit_serial_async=AsyncMock(return_value=None)),
        ),
        patch.object(server.tasks.readiness_engine, "run_device_submission_probe", probe),
        patch.object(DeviceExecutionLock, "reserve", return_value="fake-ticket"),
        patch.object(DeviceExecutionLock, "cancel_reservation", MagicMock()),
    ):
        with client.websocket_connect(
            "/api/device-bridge/session", headers={"Host": "127.0.0.1"}
        ) as socket:
            leased = socket.receive_json()
            attached = socket.receive_json()
            assert leased["type"] == "session_leased"
            assert attached["type"] == "device_attached"
            response = client.post(
                "/api/run",
                headers={"Host": "127.0.0.1"},
                json={
                    "goal": "Open Settings",
                    "device_serial": attached["serial"],
                    "bridge_session_id": leased["session_id"],
                },
            )
            print(json.dumps({"status": response.status_code, "body": response.json()}), flush=True)
            assert response.status_code == 200, response.json()
            item = response.json()["tasks"][0]
            assert item["bridge_session_id"] == leased["session_id"]
            assert item["device_serial"] == attached["serial"]
            assert item["device_binding"]["endpoint"] == AdbEndpoint.local().to_dict()
            assert queue._task_target(item, resolve_host=True).serial == attached["serial"]
            payload = {
                "goal": "Run negative control",
                "device_serial": attached["serial"],
                "bridge_session_id": "stale-lease",
            }
            stale = client.post("/api/run", headers={"Host": "127.0.0.1"}, json=payload)
            assert stale.status_code == 409
            assert stale.json()["code"] == "bridge_session_unavailable"
            payload["bridge_session_id"] = leased["session_id"]
            with patch.object(bridge, "live_sessions", return_value=[]):
                lost = client.post("/api/run", headers={"Host": "127.0.0.1"}, json=payload)
            assert lost.status_code == 409
            assert lost.json()["code"] == "bridge_queue_binding_unavailable"
            assert len(queue_module.state.queue_items) == 1
            print(json.dumps({"stale": stale.json(), "lost_at_queue": lost.json()}), flush=True)
            socket.send_text("close")
        assert asyncio.run(bridge.active_session_ids()) == set()
    assert [arguments[0] for arguments in calls] == ["connect", "disconnect"]
    print(json.dumps({"accepted": True, "cleanup": True}), flush=True)


if __name__ == "__main__":
    try:
        connect_then_run(Path(sys.argv[1]))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    os._exit(0)
