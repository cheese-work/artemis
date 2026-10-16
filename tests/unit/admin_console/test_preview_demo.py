import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import time

import pytest

from apps.admin_console.core import preview_demo
from apps.admin_console.core.preview_demo import STATES, demo_devices, visible_device_rows
from apps.admin_console.core.preview_fixtures import seed_preview_fixtures
from apps.admin_console.core.preview_profile import preview_demo_selected
from apps.admin_console.core.ownership import SYSTEM_PRINCIPAL, OwnerScope
from apps.admin_console.core.state import state
from apps.admin_console.database.repositories.run_catalog_repository import run_catalog_repo
from apps.admin_console.database.repositories.session_repository import session_repo

QA_A = "qa-a@example.test"
QA_B = "qa-b@example.test"
ADMIN = "admin@example.test"
OWNERS = (QA_A, QA_B, ADMIN)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_demo, "_devices", ())
    monkeypatch.setattr(preview_demo, "SEED_HOOKS", [])
    now = time.time()
    queue = seed_preview_fixtures(tmp_path, (QA_A, QA_B), ADMIN, demo_now=now)
    db = tmp_path / "traces" / "data_engine.db"
    monkeypatch.setattr(session_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "db_path", db)
    monkeypatch.setattr(run_catalog_repo, "traces_dir", tmp_path / "traces")
    monkeypatch.setattr(state, "queue_items", queue)
    return tmp_path, now, queue


def test_demo_state_set_is_twenty_labelled_devices():
    devices = demo_devices(QA_A, QA_B)
    assert len(devices) == 20
    assert len({d.label for d in devices}) == 20
    assert {d.state for d in devices} == set(STATES)
    assert {s: sum(d.state == s for d in devices) for s in STATES} == {
        "idle": 6, "private": 5, "busy": 4, "disconnected": 3, "unknown": 2,
    }  # fmt: skip
    assert [len(d.serials) for d in devices if len(d.serials) > 1] == [2]  # USB + Wi-Fi


def test_rows_follow_the_device_listing_contract(board):
    rows = visible_device_rows(SYSTEM_PRINCIPAL)
    assert len(rows) == 21  # 20 devices, one with two connections
    assert {tuple(row) for row in rows} == {
        ("serial", "state", "model", "product", "is_emulator", "device_kind", "is_busy",
         "active_pid", "active_task_desc", "active_session_id", "acquired_at"),
    }  # fmt: skip
    assert {row["state"] for row in rows} == {"device", "offline", "unknown"}
    busy = [row for row in rows if row["is_busy"]]
    assert len(busy) == 4
    for row in busy:
        assert row["active_session_id"] and row["active_task_desc"]  # open scope sees the run
        assert session_repo.get_session_by_id(row["active_session_id"])["status"] == "running"


def test_busy_rows_hide_runs_the_caller_cannot_see(board):
    def busy(scope):
        return [r for r in visible_device_rows(scope) if r["is_busy"]]

    running = [i for i in board[2] if i["status"] == "running" and i["device_id"]]
    assert len(running) == 4
    for email, admin in ((QA_A, False), (QA_B, False), (ADMIN, True)):
        rows = busy(OwnerScope(enforced=True, email=email, admin=admin))
        assert len(rows) == 4  # the busy indication is not hidden
        shown = {r["active_session_id"] for r in rows if r["active_session_id"]}
        assert shown == {i["session_id"] for i in running if admin or i["requested_by"] == email}
        for row in rows:
            assert (row["active_session_id"] is None) == (row["active_task_desc"] is None)


def test_private_devices_show_only_to_their_owner_and_admin(board):
    def shown(scope):
        return {r["model"] for r in visible_device_rows(scope)}

    private = {d.label: d.owner for d in demo_devices(QA_A, QA_B) if d.state == "private"}
    for email, admin in ((QA_A, False), (QA_B, False), (ADMIN, True)):
        seen = shown(OwnerScope(enforced=True, email=email, admin=admin))
        own = {label for label, owner in private.items() if admin or owner == email}
        assert seen & set(private) == own
    assert len(shown(OwnerScope(enforced=True, email=QA_A))) == 15 + 3


def test_runs_cover_failures_interrupt_busy_and_playable_evidence(board):
    root, now, queue = board
    with sqlite3.connect(root / "traces" / "data_engine.db") as conn:
        runs = conn.execute("SELECT status, start_time FROM sessions").fetchall()
    statuses = [status for status, _ in runs]
    assert statuses.count("failed") == 4 and statuses.count("interrupted") == 1
    assert [i["status"] for i in queue].count("interrupted") == 1
    assert all(abs(start - now) <= 86_400 + 60 for _, start in runs)  # stamped from boot time
    with sqlite3.connect(root / "traces" / "data_engine.db") as conn:
        reasons = conn.execute(
            "SELECT interrupt_reason FROM sessions WHERE interrupt_reason IS NOT NULL"
        ).fetchall()
    assert reasons == [("device_offline",)]
    evidence = list((root / "traces").glob("*/recording.mp4"))
    assert len(evidence) == 3
    assert all(p.read_bytes()[4:8] == b"ftyp" for p in evidence)  # a real MP4 container


def test_seed_hooks_run_last_with_context_and_extend_the_queue(tmp_path, monkeypatch):
    seen = []

    def hook(seed):
        seen.append(seed)
        return [{"session_id": "hook", "status": "pending"}]

    monkeypatch.setattr(preview_demo, "_devices", ())
    monkeypatch.setattr(preview_demo, "SEED_HOOKS", [hook])
    queue = seed_preview_fixtures(tmp_path, (QA_A, QA_B), ADMIN, demo_now=time.time())
    assert queue[-1]["session_id"] == "hook"
    assert len(seen[0].devices) == 20 and seen[0].owners == OWNERS
    assert set(seen[0].session_ids) >= {"interrupted", "busy/0", "evidence/0"}


def test_base_fixtures_are_unchanged_without_the_demo(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_demo, "_devices", ())
    assert len(seed_preview_fixtures(tmp_path, (QA_A, QA_B), ADMIN)) == 6
    assert visible_device_rows(SYSTEM_PRINCIPAL) == []


def test_demo_flag_is_validated_and_needs_the_preview_profile():
    assert preview_demo_selected(True, {"ARTEMIS_PREVIEW_DEMO": "1"}) is True
    assert preview_demo_selected(True, {}) is False
    with pytest.raises(ValueError, match="requires the isolated preview profile"):
        preview_demo_selected(False, {"ARTEMIS_PREVIEW_DEMO": "1"})
    with pytest.raises(ValueError, match="must be 0, false, 1 or true"):
        preview_demo_selected(True, {"ARTEMIS_PREVIEW_DEMO": "maybe"})


_BOOT = textwrap.dedent("""
    import json, socket, subprocess
    effects = []
    def forbidden(*args, **kwargs):
        effects.append("call")
        raise AssertionError("Preview attempted a network or process call")
    socket.socket.connect = forbidden
    class ForbiddenProcess(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            forbidden()
    subprocess.Popen = ForbiddenProcess
    import apps.admin_console.server as server
    from fastapi.testclient import TestClient
    out = {}
    with TestClient(server.app, base_url="http://localhost") as client:
        for alias in ("qa-a", "qa-b", "admin"):
            h = {"X-Artemis-Preview-Identity": alias}
            devices = client.get("/api/devices", headers=h).json()["devices"]
            runs = client.get("/api/runs?scope=available", headers=h).json()["runs"]
            out[alias] = [len(devices), len(runs)]
            ids = {r["session_id"] for r in runs}
            busy = [d for d in devices if d["is_busy"]]
            out[alias + "-busy"] = [len(busy), sum(d["active_session_id"] is None for d in busy)]
            if alias != "admin":  # scope=available lists only the caller's own runs for admin too
                assert {d["active_session_id"] for d in busy if d["active_session_id"]} <= ids
        h = {"X-Artemis-Preview-Identity": "admin"}
        done = next(r for r in client.get("/api/runs?scope=all", headers=h).json()["runs"]
                    if r["status"] == "completed" and r["session_id"] in
                    {p.parent.name for p in server.PREVIEW_ROOT.glob("traces/*/recording.mp4")})
        video = client.get(f"/api/sessions/{done['session_id']}/video", headers=h).json()
        out["video"] = video["status"]
        out["bytes"] = client.get(video["video_url"], headers=h).status_code
        out["effects"] = effects
    print(json.dumps(out))
""")


def test_real_boot_with_the_launcher_environment_serves_the_demo_board(tmp_path, request):
    launcher = Path(__file__).resolve().parents[3] / "scripts" / "preview_demo.py"
    namespace = {"__file__": str(launcher), "__name__": "launcher"}
    exec(compile(launcher.read_text(encoding="utf-8"), str(launcher), "exec"), namespace)
    root = launcher.parents[1]
    (root / ".preview-demo").mkdir(exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix="test-", dir=root / ".preview-demo"))
    request.addfinalizer(lambda: shutil.rmtree(parent, ignore_errors=True))
    result = subprocess.run(
        [sys.executable, "-c", _BOOT],
        cwd=root,
        env={
            **os.environ,
            **namespace["ENV"],
            "ARTEMIS_APP_DIR": str(parent),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["effects"] == [] and out["video"] == "ready" and out["bytes"] == 200
    assert [out[alias][0] for alias in ("qa-a", "qa-b", "admin")] == [19, 18, 21]
    # [busy rows, busy rows with the run hidden]: qa-a owns two runs, qa-b one, admin all.
    assert [out[alias + "-busy"] for alias in ("qa-a", "qa-b", "admin")] == [[4, 2], [4, 3], [4, 0]]


def test_normal_boot_refuses_the_demo_selector_before_live_imports(tmp_path):
    sentinel = tmp_path / "data_engine.db"
    sentinel.write_bytes(b"production database sentinel")
    result = subprocess.run(
        [sys.executable, "-c", "import apps.admin_console.server"],
        env={
            **os.environ,
            "ARTEMIS_PREVIEW_PROFILE": "0",
            "ARTEMIS_PREVIEW_DEMO": "1",
            "ARTEMIS_APP_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "demo board requires the isolated preview profile" in result.stderr
    assert list(tmp_path.iterdir()) == [sentinel]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_built_wheel_boots_the_demo_fixture(tmp_path):
    """The clip must ship in the wheel, not only sit in the checkout."""
    root = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"  # a clean copy, so a stale build/ directory cannot supply the clip
    files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.split("\0")  # fmt: skip
    for name in filter(None, files):
        if (root / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, source / name)
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(source)],
        capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    assert built.returncode == 0, built.stderr
    site = tmp_path / "site"
    with zipfile.ZipFile(next((tmp_path / "dist").glob("*.whl"))) as wheel:
        wheel.extractall(site)
    probe = (
        "import time, pathlib, sys;"
        "from apps.admin_console.core import preview_demo as d;"
        "from apps.admin_console.core.preview_fixtures import seed_preview_fixtures;"
        "assert d.CLIP.is_file() and str(d.CLIP).startswith(sys.argv[1]), d.CLIP;"
        "root = pathlib.Path(sys.argv[2]);"
        "seed_preview_fixtures(root, ('a@x.test', 'b@x.test'), 'c@x.test', demo_now=time.time());"
        "print(len(list(root.glob('traces/*/recording.mp4'))))"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    ran = subprocess.run(
        [sys.executable, "-c", probe, str(site), str(fixtures)],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip().splitlines()[-1] == "3"
