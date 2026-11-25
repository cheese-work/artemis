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


PY_LOCK = """version = 1
[[package]]
name = "left-pad"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/left_pad-1.2.3-py3-none-any.whl", hash = "sha256:00" }]

[[package]]
name = "artemis"
version = "1.0"
source = { editable = "." }
"""


@pytest.fixture
def pydir(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (tmp_path / "uv.lock").write_text(PY_LOCK)
    inputs = tmp_path / "python-inputs"
    (inputs / "wheelhouse").mkdir(parents=True)
    (inputs / "requirements.txt").write_text("left-pad==1.2.3 --hash=sha256:00\n")
    from scripts import preview_build

    shutil.copy(preview_build.BUILD_REQUIREMENTS, inputs / "build-requirements.txt")
    (inputs / "wheelhouse/left_pad-1.2.3-py3-none-any.whl").write_bytes(b"wheel")
    return tmp_path


def test_python_plan_exports_locked_hashes_and_downloads_binary_only(preview_build, pydir):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    export, copy_build, runtime, build = plan.commands[1:]
    assert plan.commands[0] == ("mkdir", "-p", "python-inputs/wheelhouse")
    assert export == (
        "uv", "export", "--frozen", "--offline", "--no-config", "--no-dev",
        "--no-emit-project", "--no-emit-workspace", "--format", "requirements-txt",
        "--output-file", "python-inputs/requirements.txt",
    )  # fmt: skip
    assert copy_build[:2] == ("cp", str(preview_build.BUILD_REQUIREMENTS))
    for download, source in ((runtime, "requirements"), (build, "build-requirements")):
        assert download[:3] == ("pip", "download", "--require-hashes")
        assert {"--only-binary=:all:", "--no-deps"} <= set(download)
        platforms = [download[i + 1] for i, arg in enumerate(download) if arg == "--platform"]
        assert platforms == [
            *(f"manylinux_2_{n}_x86_64" for n in range(36, 4, -1)),
            "manylinux2014_x86_64",
            "manylinux2010_x86_64",
            "manylinux1_x86_64",
        ]
        assert download[download.index("--abi") + 1] == "cp312"
        assert download[download.index("--python-version") + 1] == "3.12"
        assert download[-2:] == ("-r", f"python-inputs/{source}.txt")
    assert plan.cwd == pydir
    assert plan.env == {
        "HTTPS_PROXY": PROXY,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    }
    assert plan.pyproject_sha256 == sha256((pydir / "pyproject.toml").read_bytes()).hexdigest()
    assert plan.uv_lock_sha256 == sha256((pydir / "uv.lock").read_bytes()).hexdigest()
    assert (
        plan.build_requirements_sha256
        == sha256(preview_build.BUILD_REQUIREMENTS.read_bytes()).hexdigest()
    )


def test_build_requirements_are_pinned_and_hashed(preview_build):
    text = preview_build.BUILD_REQUIREMENTS.read_text().replace("\\\n", " ")
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert {line.split("==")[0] for line in lines} == {"setuptools", "cython"}
    for line in lines:
        assert re.fullmatch(
            r"[\w.-]+==[\w.]+( --hash=sha256:[a-f0-9]{64})+", re.sub(r"\s+", " ", line)
        )


def test_python_plan_refuses_source_only_package(preview_build, pydir):
    (pydir / "uv.lock").write_text(
        PY_LOCK.replace(
            'wheels = [{ url = "https://files.example/left_pad-1.2.3-py3-none-any.whl", hash = "sha256:00" }]',
            'sdist = { url = "https://files.example/left_pad-1.2.3.tar.gz", hash = "sha256:00" }',
        )
    )
    with pytest.raises(ValueError, match=r"left-pad==1\.2\.3.*binary wheel"):
        preview_build.acquire_python_plan(pydir, PROXY)


def _lock_with_wheels(*filenames):
    urls = ", ".join(
        f'{{ url = "https://files.example/{name}", hash = "sha256:00" }}' for name in filenames
    )
    return PY_LOCK.replace(
        'wheels = [{ url = "https://files.example/left_pad-1.2.3-py3-none-any.whl", hash = "sha256:00" }]',
        f"wheels = [{urls}]",
    )


@pytest.mark.parametrize(
    "tag",
    [
        "cp311-cp311-manylinux_2_28_x86_64",
        "cp313-cp313-manylinux_2_28_x86_64",
        "cp312-cp312-manylinux_2_28_aarch64",
        "cp312-cp312-musllinux_1_2_x86_64",
        "cp312-cp312-macosx_11_0_arm64",
        "cp312-cp312-win_amd64",
        "cp312-cp312-manylinux_2_37_x86_64",
        "cp312-cp313-manylinux_2_28_x86_64",
        "cp313-abi3-manylinux_2_28_x86_64",
        "py2-none-any",
        "py313-none-any",
    ],
)
def test_python_plan_refuses_wheels_that_do_not_fit_the_builder(preview_build, pydir, tag):
    (pydir / "uv.lock").write_text(_lock_with_wheels(f"left_pad-1.2.3-{tag}.whl"))
    with pytest.raises(ValueError, match=r"left-pad==1\.2\.3.*compatible wheel"):
        preview_build.acquire_python_plan(pydir, PROXY)


@pytest.mark.parametrize(
    "tag",
    [
        "py3-none-any",
        "py2.py3-none-any",
        "py312-none-any",
        "cp312-cp312-manylinux_2_17_x86_64",
        "cp312-cp312-manylinux1_x86_64.manylinux_2_28_x86_64",
        "cp39-abi3-manylinux2014_x86_64",
        "cp312-none-manylinux_2_36_x86_64",
        "py3-none-manylinux_2_5_x86_64",
    ],
)
def test_python_plan_accepts_a_wheel_that_fits_the_builder(preview_build, pydir, tag):
    (pydir / "uv.lock").write_text(
        _lock_with_wheels("left_pad-1.2.3-cp311-cp311-win_amd64.whl", f"left_pad-1.2.3-{tag}.whl")
    )
    assert preview_build.acquire_python_plan(pydir, PROXY).commands


WIN_ONLY = """
[[package]]
name = "winlib"
version = "9"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.example/winlib-9-cp312-cp312-win_amd64.whl", hash = "sha256:00" }]
"""


def test_python_plan_skips_a_windows_only_package_reached_only_by_marker(preview_build, pydir):
    lock = _lock_with_wheels("left_pad-1.2.3-py3-none-any.whl").replace(
        'source = { editable = "." }',
        'source = { editable = "." }\ndependencies = [{ name = "winlib", marker = "sys_platform == \'win32\'" }]',
    )
    (pydir / "uv.lock").write_text(lock + WIN_ONLY)
    assert preview_build.acquire_python_plan(pydir, PROXY).commands


def test_python_plan_refuses_a_windows_only_package_reached_without_marker(preview_build, pydir):
    lock = _lock_with_wheels("left_pad-1.2.3-py3-none-any.whl").replace(
        'source = { editable = "." }',
        'source = { editable = "." }\ndependencies = [{ name = "winlib" }]',
    )
    (pydir / "uv.lock").write_text(lock + WIN_ONLY)
    with pytest.raises(ValueError, match=r"winlib==9.*compatible wheel"):
        preview_build.acquire_python_plan(pydir, PROXY)


def test_python_plan_accepts_the_checked_in_lock(preview_build):
    root = Path(__file__).resolve().parents[3]
    assert preview_build.acquire_python_plan(root, PROXY).commands


@pytest.mark.parametrize("name", ["uv.lock", "pyproject.toml"])
def test_python_plan_refuses_missing_metadata(preview_build, pydir, name):
    (pydir / name).unlink()
    with pytest.raises(OSError):
        preview_build.acquire_python_plan(pydir, PROXY)


def test_python_plan_refuses_non_http_proxy(preview_build, pydir):
    with pytest.raises(ValueError, match="proxy"):
        preview_build.acquire_python_plan(pydir, "socks5://127.0.0.1:1")


def test_python_manifest_round_trip(preview_build, pydir):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    manifest = preview_build.stage_python_manifest(pydir, plan, "24.0")
    assert manifest["pip_version"] == "24.0"
    assert manifest["uv_lock_sha256"] == plan.uv_lock_sha256
    assert re.fullmatch(r"[a-f0-9]{64}", manifest["wheelhouse_sha256"])
    preview_build.verify_python_staged(pydir, json.loads(json.dumps(manifest)))


def test_python_stage_refuses_lock_changed_after_planning(preview_build, pydir):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    (pydir / "uv.lock").write_text(PY_LOCK + "\n")
    with pytest.raises(ValueError, match="input metadata changed"):
        preview_build.stage_python_manifest(pydir, plan, "24.0")


def test_python_stage_refuses_empty_wheelhouse(preview_build, pydir):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    (pydir / "python-inputs/wheelhouse/left_pad-1.2.3-py3-none-any.whl").unlink()
    with pytest.raises(ValueError, match="no wheels"):
        preview_build.stage_python_manifest(pydir, plan, "24.0")


@pytest.mark.parametrize(
    "tamper", ["change-wheel", "remove-wheel", "add-wheel", "change-requirements"]
)
def test_python_verify_refuses_tampered_inputs(preview_build, pydir, tamper):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    manifest = preview_build.stage_python_manifest(pydir, plan, "24.0")
    inputs = pydir / "python-inputs"
    wheel = inputs / "wheelhouse/left_pad-1.2.3-py3-none-any.whl"
    if tamper == "change-wheel":
        wheel.write_bytes(b"other")
    elif tamper == "remove-wheel":
        wheel.unlink()
    elif tamper == "add-wheel":
        (inputs / "wheelhouse/extra-1-py3-none-any.whl").write_bytes(b"x")
    else:
        (inputs / "requirements.txt").write_text("evil==1 --hash=sha256:00\n")
    with pytest.raises(ValueError, match="staged input differs"):
        preview_build.verify_python_staged(pydir, manifest)


def test_python_verify_refuses_invalid_manifest(preview_build, pydir):
    plan = preview_build.acquire_python_plan(pydir, PROXY)
    manifest = preview_build.stage_python_manifest(pydir, plan, "24.0")
    with pytest.raises(ValueError, match="pip version"):
        preview_build.verify_python_staged(pydir, {**manifest, "pip_version": " "})


def test_preview_installs_python_only_from_the_verified_wheelhouse():
    stages = _stages()
    assert (
        stages["preview-python-inputs"].split(b"\n")[0] == b"FROM scratch AS preview-python-inputs"
    )
    reachable = _reachable(stages, "preview")
    assert {"preview-python-inputs", "preview-wheel-builder"} <= reachable
    wheel_runs = re.sub(r"\\\n\s*", " ", stages["preview-wheel-builder"].decode())
    for project in ("./packages/artemis-client", "."):
        assert re.search(
            rf"^RUN --network=none .*pip wheel --no-cache-dir --no-index --find-links \S+ "
            rf"--no-build-isolation --no-deps --wheel-dir /wheels {re.escape(project)}(?: +&&|$)",
            wheel_runs,
            re.M,
        ), project
    assert "pip install --no-cache-dir --no-index --require-hashes" in wheel_runs
    preview = re.sub(r"\\\n\s*", " ", stages["preview"].decode())
    assert re.search(
        r"^RUN --network=none .*from=preview-python-inputs.* pip install .*--no-index --require-hashes .*-r \S*requirements\.txt",
        preview,
        re.M,
    )
    assert re.search(
        r"pip install .*--no-index --no-deps /wheels/artemis_client-\S+\.whl /wheels/artemis-\S+\.whl",
        preview,
    )


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
