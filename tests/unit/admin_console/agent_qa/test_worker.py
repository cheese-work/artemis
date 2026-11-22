import hashlib
from io import BytesIO
import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from apps.admin_console.schemas.task_schema import RunRequest
from apps.admin_console.core.access_control import AccessIdentity, AdminAPIError
from apps.admin_console.core.ownership import OwnerScope
from apps.admin_console.routers import tasks
from apps.admin_console.services.agent_qa import binding, worker
from apps.admin_console.services.agent_qa.flow import build_flow
from apps.admin_console.services.agent_qa.gate import evaluate_gate
from apps.admin_console.services.agent_qa.session import load_session
from apps.admin_console.services.device_leases import Lease
from apps.admin_console.services.task_queue_service import TaskQueueService
from artemis.runtime.adb_endpoint import AdbEndpoint, AdbTarget


PACKAGE = "example.app"
DIGEST = hashlib.sha256(b"apk").hexdigest()
SERIAL = "emulator-5554"


def request_payload():
    return {
        "goal": "Replay the SmartQA route",
        "locked_app_package": PACKAGE,
        "options": {
            "smartqa": {
                "theme": "dark",
                "lease_id": "lease",
                "apk_sha256": DIGEST,
                "fresh_install": True,
                "clear_data": True,
                "mode": "route",
                "screens": ["home"],
                "max_model_calls": 0,
                "route": [{"action": "tap", "id": "home"}, {"action": "back"}],
            }
        },
    }


class FakeTransport:
    endpoint = AdbEndpoint.local()

    def __init__(self, installed=DIGEST):
        self.calls = []
        self.installed = installed
        self.theme = "auto"
        self.fail_action = False

    def run(self, argv, **kwargs):
        assert argv[:2] == ["-s", SERIAL]
        command = tuple(argv[2:])
        self.calls.append(command)
        if command[:4] == ("shell", "cmd", "uimode", "night"):
            if len(command) == 5:
                self.theme = command[4]
            output = f"Night mode: {self.theme}".encode()
        elif command[:3] == ("shell", "pm", "path"):
            output = b"package:/data/app/example/base.apk\n"
        elif command[:2] == ("shell", "sha256sum"):
            output = f"{self.installed}  /data/app/example/base.apk\n".encode()
        elif command == ("shell", "date", "+%s.%N"):
            output = b"1710000000.000000000\n"
        elif command[:2] == ("shell", "logcat"):
            output = (
                b"[0,301,example.app,2,private details]\n[0,302,unrelated.app,2,private]\n"
                if "am_anr:I" in command
                else b"FATAL EXCEPTION: main\nProcess: example.app, PID: 99\nprivate stack\n"
            )
            if self.fail_action:
                output = b""
        elif command[:2] == ("install", "-r") or command[:3] == ("shell", "pm", "clear"):
            output = b"Success\n"
        elif command[:3] == ("shell", "cmd", "package"):
            output = b"example.app/.MainActivity\n"
        elif command[:3] == ("shell", "am", "start"):
            output = b"Status: ok\n"
        elif command[:3] == ("shell", "dumpsys", "activity"):
            output = b"topResumedActivity=ActivityRecord{abc u0 example.app/.MainActivity t2}\n"
        elif command[:2] == ("exec-out", "cat"):
            output = b'<hierarchy><node package="example.app" resource-id="example.app:id/home" text="Home" bounds="[1,1][35,35]" /></hierarchy>'
        elif command == ("exec-out", "screencap", "-p"):
            assert self.theme == "yes"
            image = Image.new("RGB", (40, 40), "white")
            image.putpixel((6, 6), (0, 0, 0))
            stream = BytesIO()
            image.save(stream, format="PNG")
            output = stream.getvalue()
        else:
            output = b""
        return CompletedProcess(
            argv, int(self.fail_action and command[:3] == ("shell", "input", "tap")), output, b""
        )


def run(tmp_path: Path, transport: FakeTransport, **kwargs):
    request = RunRequest.model_validate(request_payload())
    apk = tmp_path / f"{DIGEST}.apk"
    apk.write_bytes(b"apk")
    worker.record_route(
        request.options.smartqa, PACKAGE, apk, SERIAL, transport, tmp_path, **kwargs
    )
    return tmp_path / "smartqa"


def test_dark_route_records_report_input(tmp_path):
    transport = FakeTransport()
    directory = run(tmp_path, transport)
    session = load_session(directory)
    theme = session.themes["dark"]
    assert theme.installed_apk_sha256 == DIGEST
    assert [step.action["type"] for step in theme.steps] == ["tap", "back"]
    assert all(step.screen_after == "home" for step in theme.steps)
    assert theme.steps[0].text_nodes[0]["text"] == "Home"
    assert len(build_flow(session, "dark").steps) == 2
    assert [rule.passed for rule in evaluate_gate(session).rules[:2]] == [False, False]
    assert "private" not in (directory / "session.json").read_text()
    assert transport.calls.count(("shell", "sha256sum", "/data/app/example/base.apk")) == 2
    assert transport.theme == "auto"


@pytest.mark.parametrize("installed", ["", "0" * 64])
def test_missing_or_wrong_installed_digest_cannot_publish(tmp_path, installed):
    transport = FakeTransport(installed)
    with pytest.raises(worker.SmartQAError, match="installed_apk"):
        run(tmp_path, transport)
    assert not (tmp_path / "smartqa").exists()
    assert transport.theme == "auto"


def test_route_failure_restores_theme(tmp_path):
    transport = FakeTransport()
    transport.fail_action = True
    with pytest.raises(worker.SmartQAError):
        run(tmp_path, transport)
    assert transport.theme == "auto"
    assert not (tmp_path / "smartqa").exists()


def test_cancel_marker_restores_theme(tmp_path):
    transport = FakeTransport()
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, transport, cancelled=lambda: transport.theme == "yes")
    assert ("shell", "cmd", "uimode", "night", "yes") in transport.calls
    assert transport.theme == "auto"
    assert not (tmp_path / "smartqa").exists()


def test_schema_does_not_drop_route():
    request = RunRequest.model_validate_json(json.dumps(request_payload()))
    assert request.options.smartqa.theme == "dark"
    assert request.options.smartqa.route[0].id == "home"


@pytest.mark.parametrize("holder,restricted", [("owner", True), ("other", False), (None, False)])
def test_lease_refusal_does_not_reach_device(monkeypatch, holder, restricted):
    monkeypatch.setattr(binding, "_enabled", lambda: True)
    monkeypatch.setattr(binding, "device_leases_enabled", lambda: True)
    lease = Lease("lease", "device", holder, "smartqa", restricted, None, 0, 100)
    monkeypatch.setattr(binding.device_leases, "active", lambda _: [lease] if holder else [])
    transport = FakeTransport()
    options = RunRequest.model_validate(request_payload()).options.smartqa
    with pytest.raises(AdminAPIError, match="dedicated local emulator"):
        binding.verify_device(options, "owner", transport, SERIAL)
    assert not transport.calls


def test_queue_forwards_dark_route_to_worker():
    options = RunRequest.model_validate(request_payload()).options.smartqa
    job = {"options": options.model_dump(), "holder": "owner", "package": PACKAGE}
    args, env = TaskQueueService._build_worker_invocation(
        {"smartqa_job": job, "queue_ticket": "ticket"},
        "run",
        "session",
        "goal",
        "pro",
        AdbTarget(AdbEndpoint.local(), SERIAL),
        base_environment={},
    )
    assert args[1:] == ["-m", "apps.admin_console.services.agent_qa.worker"]
    assert env["ADB_DEVICE_SERIAL"] == SERIAL
    assert env["ARTEMIS_SESSION_ID"] == "session"
    assert json.loads(env["ARTEMIS_SMARTQA_WORKER"]) == job


@pytest.mark.asyncio
async def test_run_admission_forwards_dark_route_and_bound_serial(monkeypatch):
    def bind(options, holder, transport):
        assert options.lease_id == "lease"
        assert holder == "smartqa-ci"
        assert transport.endpoint.is_local_default
        return SERIAL

    enqueue = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(binding, "verify_device", bind)
    monkeypatch.setattr(binding, "stored_apk", lambda _: Path("stored.apk"))
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue)
    monkeypatch.setattr(tasks.task_queue_service, "require_admission_open", lambda: None)
    discovery = AsyncMock(side_effect=AssertionError("Scripted runs must not resolve a model"))
    monkeypatch.setattr(tasks.model_discovery, "resolve", discovery)
    request = RunRequest.model_validate(request_payload())
    result = await tasks.run_task(
        request,
        OwnerScope(enforced=True),
        AccessIdentity(None, False, "cloudflare", service_principal="smartqa-ci"),
    )
    job = enqueue.await_args.kwargs["smartqa_job"]
    assert result == {"status": "queued"}
    assert enqueue.await_args.kwargs["device_serial"] == SERIAL
    assert job["options"]["theme"] == "dark"
    assert job["options"]["route"][0]["id"] == "home"
    discovery.assert_not_awaited()
