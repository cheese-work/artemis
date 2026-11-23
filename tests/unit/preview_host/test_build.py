"""Hermetic checks for Node acquisition and the offline preview target."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
PROXY = "http://127.0.0.1:3128"
LIVE_STAGE_DIGESTS = {
    "frontend-builder": "e667761773cbd7cdc820d69c9bb4056caeed3c5269ed323ba469a5f844269389",
    "wheel-builder": "4efb69cfa11cf33d1aa86b73f8853acc67cd46bb7c204d55b8a4d4ac9ceaf632",
    "runtime-base": "891e07ef9aa7c6f95668bc2bfb644c51c861d6f02925d0fab239d4dbcc7cbd88",
    "<default>": "740ec6da2592073b281f0c90eebe0b79f8f5396cb713304604a1bc1b98e65ae0",
}


@pytest.fixture
def preview_build():
    from scripts import preview_build

    return preview_build


@pytest.fixture
def app(tmp_path):
    app_dir = tmp_path / "apps/showcase_ui"
    package = app_dir / "node_modules/a-tool"
    package.mkdir(parents=True)
    executable = package / "cli.js"
    executable.write_text("#!/usr/bin/env node\n")
    executable.chmod(0o755)
    bins = app_dir / "node_modules/.bin"
    bins.mkdir()
    (bins / "a-tool").symlink_to("../a-tool/cli.js")
    (app_dir / "package.json").write_text('{"name":"fixture"}')
    (app_dir / "package-lock.json").write_text('{"lockfileVersion":3}')
    return app_dir


def test_acquire_plan_is_hook_free_and_uses_only_given_proxy(
    preview_build, tmp_path, app, monkeypatch
):
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("npm_config_registry", "https://other.invalid/")
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)

    assert plan.cwd == app
    assert plan.argv == ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    assert plan.env == {
        "HTTPS_PROXY": PROXY,
        "npm_config_https_proxy": PROXY,
        "npm_config_userconfig": "/dev/null",
        "npm_config_globalconfig": "/proc/self/root/dev/null",
    }
    assert plan.package_json_sha256 == sha256((app / "package.json").read_bytes()).hexdigest()
    assert plan.lockfile_sha256 == sha256((app / "package-lock.json").read_bytes()).hexdigest()


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not installed")
def test_npm_accepts_the_isolated_config(preview_build, tmp_path, app):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    version = subprocess.check_output(
        [plan.argv[0], "--version"],
        cwd=plan.cwd,
        env={**plan.env, "PATH": os.environ["PATH"]},
        text=True,
        timeout=10,
    )
    assert re.match(r"\d+\.\d+\.\d+", version)


@pytest.mark.parametrize(
    "lockfile", [None, '{"lockfileVersion":2}', '{"lockfileVersion":3.0}', "{"]
)
def test_acquire_refuses_bad_lockfiles(preview_build, tmp_path, app, lockfile):
    path = app / "package-lock.json"
    if lockfile is None:
        path.unlink()
    else:
        path.write_text(lockfile)
    with pytest.raises(ValueError, match="package-lock.json"):
        preview_build.acquire_node_plan(tmp_path, PROXY)


def test_acquire_refuses_project_npmrc(preview_build, tmp_path, app):
    (app / ".npmrc").write_text("noproxy=*\n")
    with pytest.raises(ValueError, match="project .npmrc"):
        preview_build.acquire_node_plan(tmp_path, PROXY)


def test_manifest_round_trip_binds_plan_and_staged_tree(preview_build, tmp_path, app):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    manifest = preview_build.stage_manifest(tmp_path, plan, "10.9.9")
    assert manifest["package_json_sha256"] == plan.package_json_sha256
    assert manifest["lockfile_sha256"] == plan.lockfile_sha256
    assert manifest["npm_version"] == "10.9.9"
    assert re.fullmatch(r"[a-f0-9]{64}", manifest["node_modules_sha256"])
    preview_build.verify_staged(tmp_path, json.loads(json.dumps(manifest)))


def test_stage_refuses_locks_changed_after_planning(preview_build, tmp_path, app):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    (app / "package-lock.json").write_text('{"lockfileVersion":3,"name":"changed"}')
    with pytest.raises(ValueError, match="input metadata changed"):
        preview_build.stage_manifest(tmp_path, plan, "10.9.9")


@pytest.mark.parametrize(
    "name", ["package-lock.json", "package.json", "node_modules/a-tool/cli.js"]
)
def test_verify_refuses_changed_input(preview_build, tmp_path, app, name):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    manifest = preview_build.stage_manifest(tmp_path, plan, "10.9.9")
    path = app / name
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="staged input differs"):
        preview_build.verify_staged(tmp_path, manifest)


def test_missing_dependency_stops_before_build(preview_build, tmp_path, app):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    manifest = preview_build.stage_manifest(tmp_path, plan, "10.9.9")
    (app / "node_modules/.bin/a-tool").unlink()
    (app / "node_modules/a-tool/cli.js").unlink()
    (app / "node_modules/a-tool").rmdir()
    with pytest.raises(ValueError, match="staged input differs"):
        preview_build.verify_staged(tmp_path, manifest)


def test_staging_rejects_escaping_symlink(preview_build, tmp_path, app):
    plan = preview_build.acquire_node_plan(tmp_path, PROXY)
    (app / "node_modules/escape").symlink_to("../package.json")
    with pytest.raises(ValueError, match="symlink"):
        preview_build.stage_manifest(tmp_path, plan, "10.9.9")


def _stages() -> dict[str, bytes]:
    source = (ROOT / "Dockerfile").read_bytes()
    starts = list(re.finditer(rb"^FROM\s+\S+(?:\s+AS\s+(\S+))?$", source, re.M))
    return {
        start[1].decode() if start[1] else "<default>": source[
            start.start() : (starts[index + 1].start() if index + 1 < len(starts) else len(source))
        ]
        for index, start in enumerate(starts)
    }


def _reachable(stages: dict[str, bytes], target: str) -> set[str]:
    remaining = [target]
    seen: set[str] = set()
    while remaining:
        stage = remaining.pop()
        if stage not in seen:
            seen.add(stage)
            source = stages[stage].decode()
            base = source.splitlines()[0].split()[1]
            inputs = re.findall(r"(?:^COPY\s+--from=|\bfrom=)([\w-]+)", source, re.M)
            remaining.extend(name for name in [base, *inputs] if name in stages)
    return seen


def test_live_target_unchanged_from_base():
    stages = _stages()
    assert next(reversed(stages)) == "<default>"
    assert {name: sha256(stages[name]).hexdigest() for name in _reachable(stages, "<default>")} == (
        LIVE_STAGE_DIGESTS
    )
    assert "apps/showcase_ui/node_modules" in (ROOT / ".dockerignore").read_text().splitlines()


def test_preview_reachable_runs_are_offline_and_keep_the_runtime_contract():
    stages = _stages()
    reachable = _reachable(stages, "preview")
    assert reachable.isdisjoint(LIVE_STAGE_DIGESTS)
    runs = []
    for stage in reachable:
        source = stages[stage].decode().replace("\\\n", " ")
        for command in re.findall(r"^RUN\s+(.+)$", source, re.M):
            assert command.startswith("--network=none "), (stage, command)
            assert not re.search(r"\b(npm\s+(ci|install)|curl|git|apt-get)\b", command)
            if re.search(r"\bpip\s+install\b", command):
                assert "--no-index" in command
            runs.append(command)
    for expected in ("npm rebuild", "npm run postinstall", "npm run build"):
        assert any(expected in run for run in runs)
    frontend = stages["preview-frontend-builder"]
    assert frontend.index(b"(googleapis|gstatic)") < frontend.index(b"npm run build")
    for name in (b"package.json", b"package-lock.json", b"node_modules/"):
        assert any(
            line.startswith(b"COPY --from=preview-node-inputs ")
            and b"/apps/showcase_ui/" + name in line
            for line in frontend.splitlines()
        )
    preview = stages["preview"].splitlines()
    assert b"USER 10001:10001" in preview
    assert b"ENV ARTEMIS_PREVIEW_PROFILE=1" in preview
    assert (
        preview.count(
            b'CMD ["uvicorn", "apps.admin_console.server:app", "--host", "0.0.0.0", "--port", "8080"]'
        )
        == 1
    )
    assert any(
        line.startswith(b"COPY --from=preview-frontend-builder ")
        and line.endswith(b"./apps/showcase_ui/dist/")
        for line in preview
    )
