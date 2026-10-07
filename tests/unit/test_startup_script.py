import hashlib
import os
from pathlib import Path
import subprocess
import tarfile

import pytest


PORTABLE_SHA256 = "ad56ae8bfeedf41e824945c11dbf55fcb092b3e615b9b486f48a50e30d389635"


def scrcpy_startup_sections():
    startup_script = (Path(__file__).resolve().parents[2] / "start.sh").read_text(encoding="utf-8")
    helper_start = startup_script.index('SCRCPY_VERSION=""')
    helper_end = startup_script.index("# 2. Check or install uv", helper_start)
    fallback_start = startup_script.index("# Fallback: if scrcpy is still missing")
    fallback_end = startup_script.index("# Fallback: if adb is missing", fallback_start)
    return startup_script[helper_start:helper_end], startup_script[fallback_start:fallback_end]


def write_scrcpy(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf 'scrcpy {version}\\n'\n", encoding="utf-8")
    path.chmod(0o755)


def test_linux_portable_scrcpy_fallback_sets_resolver_override():
    _, scrcpy_fallback = scrcpy_startup_sections()

    assert 'export ARTEMIS_SCRCPY_PATH="${SCRCPY_DIR}/scrcpy"' in scrcpy_fallback
    assert PORTABLE_SHA256 in scrcpy_fallback
    assert "sha256sum -c" in scrcpy_fallback


@pytest.mark.parametrize(
    ("version", "supported"),
    [("1.25", False), ("2.3.1", False), ("2.4", True), ("3.0", True), ("4.1", True)],
)
def test_startup_scrcpy_version_gate(tmp_path, version, supported):
    helper, _ = scrcpy_startup_sections()
    binary = tmp_path / "scrcpy"
    write_scrcpy(binary, version)
    env = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}
    env.pop("ARTEMIS_SCRCPY_PATH", None)
    result = subprocess.run(["bash", "-c", helper + "scrcpy_is_supported"], env=env)

    assert (result.returncode == 0) is supported


@pytest.mark.parametrize("cached_version", [None, "1.25", "4.1"])
def test_portable_scrcpy_replaces_old_selection_in_isolated_home(tmp_path, cached_version):
    helper, fallback = scrcpy_startup_sections()
    home = tmp_path / "home"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    write_scrcpy(binary_dir / "scrcpy", "1.25")
    portable = home / ".local/share/scrcpy/4.1/scrcpy"
    if cached_version:
        write_scrcpy(portable, cached_version)

    payload = tmp_path / "payload/scrcpy"
    write_scrcpy(payload, "4.1")
    archive = tmp_path / "portable.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(payload, arcname="scrcpy-linux-x86_64-v4.1/scrcpy")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    download_log = tmp_path / "download.log"
    curl = binary_dir / "curl"
    curl.write_text(
        '#!/bin/sh\nprintf "download\\n" >> "$DOWNLOAD_LOG"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then cp "$FAKE_ARCHIVE" "$2"; exit; fi\n'
        "  shift\ndone\nexit 1\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{binary_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
        "ARTEMIS_SCRCPY_PATH": str(binary_dir / "scrcpy"),
        "FAKE_ARCHIVE": str(archive),
        "DOWNLOAD_LOG": str(download_log),
    }
    script = (
        "set -euo pipefail\nCYAN=''\nGREEN=''\nYELLOW=''\nNC=''\n"
        + helper
        + fallback.replace(PORTABLE_SHA256, digest)
        + 'printf "SELECTED=%s\\n" "$ARTEMIS_SCRCPY_PATH"\nscrcpy_is_supported\n'
    )
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"SELECTED={portable}" in result.stdout
    assert subprocess.check_output([str(portable), "--version"], text=True).strip() == "scrcpy 4.1"
    assert download_log.exists() is (cached_version != "4.1")


def test_portable_scrcpy_does_not_extract_unverified_download(tmp_path):
    helper, fallback = scrcpy_startup_sections()
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    write_scrcpy(binary_dir / "scrcpy", "1.25")
    curl = binary_dir / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then printf "tampered" > "$2"; exit; fi\n'
        "  shift\ndone\nexit 1\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    home = tmp_path / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{binary_dir}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path),
    }
    env.pop("ARTEMIS_SCRCPY_PATH", None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -euo pipefail\nCYAN=''\nGREEN=''\nYELLOW=''\nNC=''\n" + helper + fallback,
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / ".local/share/scrcpy/4.1/scrcpy").exists()
    assert "verification failed" in result.stdout
