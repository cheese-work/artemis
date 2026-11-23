# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""IPC discovery must not attach a source checkout to another installation."""

import os
from unittest.mock import MagicMock
import urllib.request

import pytest

from artemis.config import paths, runtime


@pytest.fixture
def ipc_files(tmp_path, monkeypatch):
    for name in (
        "ARTEMIS_APP_DIR",
        "ANTIGRAVITY_APP_DIR",
        "ARTEMIS_USE_USER_DIR",
        "ARTEMIS_IPC_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    checkout = tmp_path / "checkout"
    (checkout / "artemis").mkdir(parents=True)
    (checkout / "pyproject.toml").touch()
    app_dir = tmp_path / "shared-app"
    app_dir.mkdir()
    temp_dir = tmp_path / "shared-temp"
    temp_dir.mkdir()
    monkeypatch.setattr(paths, "ROOT_DIR", checkout)
    monkeypatch.setattr(runtime, "ROOT_DIR", checkout)
    monkeypatch.setattr(paths, "is_frozen_bundle", lambda: False)
    monkeypatch.setattr(paths.platform.paths, "resolve_app_dir", lambda: app_dir)
    monkeypatch.setattr(runtime, "get_temp_dir", lambda: temp_dir)
    return (
        checkout / paths.IPC_PORT_FILENAME,
        app_dir / paths.IPC_PORT_FILENAME,
        temp_dir / "artemis-ipc-port",
    )


@pytest.fixture
def status_api(monkeypatch):
    urlopen = MagicMock()
    response = urlopen.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = b'{"ipc_port": 9999}'
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return urlopen


@pytest.mark.parametrize("other_source", ["shared", "temp", "status"])
@pytest.mark.parametrize("local_port", [None, 43123])
def test_source_ipc_ignores_other_install_sources(ipc_files, status_api, other_source, local_port):
    local_file, shared_file, temp_file = ipc_files
    if other_source == "shared":
        shared_file.write_text("9999", encoding="utf-8")
    elif other_source == "temp":
        temp_file.write_text("9999", encoding="utf-8")
    if local_port is not None:
        local_file.write_text(str(local_port), encoding="utf-8")

    assert runtime.read_ipc_port() == local_port
    status_api.assert_not_called()


@pytest.mark.parametrize("operation", ["write", "clear"])
@pytest.mark.parametrize("shared_exists", [False, True])
def test_source_ipc_lifecycle_leaves_other_installs_untouched(
    ipc_files, monkeypatch, operation, shared_exists
):
    local_file, shared_file, temp_file = ipc_files
    local_file.write_text("43123", encoding="utf-8")
    if shared_exists:
        shared_file.write_text("9999", encoding="utf-8")
        temp_file.write_text("9998", encoding="utf-8")
    if operation == "write":
        assert runtime.write_ipc_port(43124) == local_file
        assert local_file.read_text(encoding="utf-8") == "43124"
        assert os.environ["ARTEMIS_IPC_PORT"] == "43124"
    else:
        monkeypatch.setenv("ARTEMIS_IPC_PORT", "43123")
        runtime.clear_ipc_port()
        assert not local_file.exists()
        assert "ARTEMIS_IPC_PORT" not in os.environ

    assert shared_file.exists() == shared_exists
    assert temp_file.exists() == shared_exists
    if shared_exists:
        assert shared_file.read_text(encoding="utf-8") == "9999"
        assert temp_file.read_text(encoding="utf-8") == "9998"


def test_source_ipc_explicit_override_is_authoritative(ipc_files, monkeypatch, status_api):
    local_file, shared_file, temp_file = ipc_files
    for file in ipc_files:
        file.write_text("9999", encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_IPC_PORT", " 43125 ")
    assert runtime.read_ipc_port() == 43125
    status_api.assert_not_called()


@pytest.mark.parametrize(
    "mode", ["wheel", "frozen", "ARTEMIS_APP_DIR", "ANTIGRAVITY_APP_DIR", "ARTEMIS_USE_USER_DIR"]
)
def test_user_app_ipc_preserves_legacy_discovery_and_lifecycle(
    ipc_files, monkeypatch, status_api, mode
):
    local_file, shared_file, temp_file = ipc_files
    if mode == "wheel":
        (local_file.parent / "pyproject.toml").unlink()
    elif mode == "frozen":
        monkeypatch.setattr(paths, "is_frozen_bundle", lambda: True)
    else:
        monkeypatch.setenv(
            mode, "true" if mode == "ARTEMIS_USE_USER_DIR" else str(shared_file.parent)
        )

    shared_file.write_text("43123", encoding="utf-8")
    temp_file.write_text("43124", encoding="utf-8")
    assert runtime.read_ipc_port() == 43124
    temp_file.unlink()
    assert runtime.read_ipc_port() == 43123
    shared_file.unlink()
    assert runtime.read_ipc_port() == 9999
    status_api.assert_called_once()

    assert runtime.write_ipc_port(43125) == shared_file
    assert runtime.read_ipc_port() == 43125
    assert shared_file.read_text(encoding="utf-8") == "43125"
    assert temp_file.read_text(encoding="utf-8") == "43125"
    assert local_file.exists() == (mode != "wheel")

    runtime.clear_ipc_port()
    assert not any(file.exists() for file in ipc_files)
    assert "ARTEMIS_IPC_PORT" not in os.environ
