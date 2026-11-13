"""Protocol golden files captured from the pinned adb, adbutils and uiautomator2 (CHE-1094).

The fake adb server (``tests/support/fake_adb.py``) is only as good as the
requests it has seen real clients send. This module drives the pinned clients
against it and compares the recorded smart-socket requests with
``tests/support/golden/adb_protocol.json``. A client upgrade that changes what
goes over the wire fails here first, before the tunnel's request filter (a later
slice) silently rejects it.

Regenerate after a deliberate upgrade::

    ARTEMIS_UPDATE_ADB_FIXTURES=1 uv run pytest tests/unit/runtime/test_adb_protocol_golden.py

The adb *binary* scenarios compare only when the installed platform-tools
version equals the golden file's; other versions skip them (adbutils and
uiautomator2 are pinned by ``uv.lock`` and always compare).
"""

from __future__ import annotations

from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import adbutils
from adbutils import AdbClient, Network
import pytest
import uiautomator2

from artemis.runtime.adb_endpoint import AdbSession

GOLDEN = Path(__file__).resolve().parents[2] / "support" / "golden" / "adb_protocol.json"
UPDATE = os.environ.get("ARTEMIS_UPDATE_ADB_FIXTURES") == "1"
REAL_ADB = shutil.which("adb")
SERIAL = "emulator-5554"


def _adb_binary_version() -> str | None:
    if REAL_ADB is None:
        return None
    out = subprocess.run([REAL_ADB, "version"], capture_output=True, text=True, timeout=15).stdout
    match = re.search(r"Version (\S+)", out)
    return match.group(1) if match else None


def _fake(fake_adb_server_factory):
    server = fake_adb_server_factory("golden")
    server.add_device(SERIAL)
    return server


def _requests(server) -> list[str]:
    return server.request_strings()


# --------------------------------------------------------------------------- #
# Scenarios: name -> callable(server) that drives one pinned client
# --------------------------------------------------------------------------- #


def _adbutils_scenarios(server) -> dict[str, list[str]]:
    client = AdbClient(server.host, server.port)
    device = client.device(SERIAL)
    recorded: dict[str, list[str]] = {}

    def capture(name, action):
        server.clear_requests()
        action()
        recorded[name] = _requests(server)

    capture("server_version", client.server_version)
    capture("device_list", client.device_list)
    capture("shell", lambda: device.shell("echo hi"))
    capture("exec_screencap_stream", lambda: device.shell("screencap -p", stream=True).close())
    server.devices[SERIAL].services[7912] = lambda _d, conn: conn.close()
    capture("tcp_stream", lambda: device.create_connection(Network.TCP, 7912).close())
    capture("forward_list", lambda: list(client.forward_list()))
    capture("connect", lambda: client.connect("127.0.0.1:40000"))
    capture("disconnect", lambda: client.disconnect("127.0.0.1:40000"))
    return recorded


def _adb_binary_scenarios(server) -> dict[str, list[str]]:
    session = AdbSession(server.endpoint, adb_path=REAL_ADB)
    recorded: dict[str, list[str]] = {}
    scenarios = {
        "devices_long": ["devices", "-l"],
        "get_state": ["-s", SERIAL, "get-state"],
        "shell": ["-s", SERIAL, "shell", "echo", "x"],
        "exec_out_screencap": ["-s", SERIAL, "exec-out", "screencap", "-p"],
        "forward_list": ["forward", "--list"],
        "connect": ["connect", "127.0.0.1:40000"],
    }
    for name, args in scenarios.items():
        server.clear_requests()
        session.run(args, capture_output=True, timeout=15)
        recorded[name] = _requests(server)
    return recorded


def _uiautomator2_scenarios(server) -> dict[str, list[str]]:
    """What ``u2.connect`` sends before it needs adb's sync service (unsupported here)."""
    server.clear_requests()
    device = AdbClient(server.host, server.port).device(SERIAL)
    with pytest.raises(adbutils.AdbError):
        uiautomator2.connect(device)
    return {"connect_until_sync": _requests(server)}


def _load() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def _save(data: dict) -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check(kind: str, versions: dict[str, str | None], recorded: dict[str, list[str]]) -> None:
    golden = _load()
    if UPDATE:
        golden.setdefault("versions", {}).update(versions)
        golden[kind] = recorded
        _save(golden)
        return
    assert kind in golden, f"no golden for {kind}; run with ARTEMIS_UPDATE_ADB_FIXTURES=1"
    pinned = golden["versions"]
    for key, value in versions.items():
        assert pinned.get(key) == value, (
            f"{key} is {value}, golden captured with {pinned.get(key)}; "
            "regenerate with ARTEMIS_UPDATE_ADB_FIXTURES=1 after reviewing the diff"
        )
    assert recorded == golden[kind]


def test_adbutils_requests_match_the_golden(fake_adb_server_factory):
    recorded = _adbutils_scenarios(_fake(fake_adb_server_factory))

    _check("adbutils", {"adbutils": package_version("adbutils")}, recorded)


def test_uiautomator2_connect_requests_match_the_golden(fake_adb_server_factory):
    recorded = _uiautomator2_scenarios(_fake(fake_adb_server_factory))

    _check(
        "uiautomator2",
        {"adbutils": package_version("adbutils"), "uiautomator2": package_version("uiautomator2")},
        recorded,
    )


@pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")
def test_adb_binary_requests_match_the_golden(fake_adb_server_factory):
    version = _adb_binary_version()
    golden_version = _load().get("versions", {}).get("adb")
    if not UPDATE and version != golden_version:
        pytest.skip(f"platform-tools {version} differs from the golden's {golden_version}")

    recorded = _adb_binary_scenarios(_fake(fake_adb_server_factory))

    _check("adb", {"adb": version}, recorded)


def test_golden_is_complete_and_names_the_pinned_clients():
    golden = _load()

    assert {"adbutils", "uiautomator2", "adb"} <= golden.keys()
    assert golden["versions"]["adbutils"] == package_version("adbutils")
    assert golden["versions"]["uiautomator2"] == package_version("uiautomator2")
    assert sys.version_info >= (3, 10)
