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

"""Repository-wide pytest fixtures and classification helpers.

The default test paths contain only deterministic tests.  Tests under the
integration and end-to-end trees remain directly runnable, and receive stable
markers here so callers can select them without relying on filename patterns.
"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from artemis.drivers.mock.mock_driver import MockDeviceDriver

PROVIDER_CREDENTIAL_FIELDS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GCP_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "OCR_API_KEY",
    "VISION_API_KEY",
    "API_KEY",
    "TYPESAFE_API_KEY",
)
FAKE_PROVIDER_CREDENTIAL_FIELDS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GCP_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "OPEN_ROUTER_API_KEY",
)


@pytest.fixture(autouse=True)
def isolate_provider_credentials(monkeypatch):
    """Keep the deterministic suite independent of ambient credentials."""
    from artemis.config.settings import settings

    for field in PROVIDER_CREDENTIAL_FIELDS:
        monkeypatch.delenv(field, raising=False)
        if hasattr(settings, field):
            monkeypatch.setattr(settings, field, None)


@pytest.fixture(autouse=True)
def isolate_current_data_engine():
    """``DataEngine.start_session`` publishes ``self`` to a module global.

    The logger drain thread and tools read it later, so an engine left over from an earlier
    test would receive this test's log records and open IPC sockets / call ``urlopen`` mid-test.
    Teardown also waits for a drain callback that already captured the engine.
    """
    from tests.support.data_engine_isolation import quiesce_data_engine_logs

    quiesce_data_engine_logs()
    yield
    quiesce_data_engine_logs()


ADB_ENVIRONMENT_KEYS = (
    "ADB_HOST",
    "ADB_PORT",
    "ADB_SERVER_SOCKET",
    "ANDROID_ADB_SERVER_HOST",
    "ANDROID_ADB_SERVER_PORT",
    "ARTEMIS_ADB_ENDPOINT_ID",
    "ARTEMIS_ADB_HOST_ID",
    "ARTEMIS_ADB_GENERATION",
    "ARTEMIS_HOST_AGENT",
)


@pytest.fixture(autouse=True)
def isolate_adb_environment():
    """Undo adb endpoint activation (``AdbEndpoint.apply_to_environment`` writes ``os.environ``).

    Process-wide endpoint selection is real production behaviour, so tests that exercise
    it must not leak the selected server into the next test: device-lock scopes and every
    adb call derive from it.
    """
    import os

    from artemis.config.settings import settings

    saved = {key: os.environ.get(key) for key in ADB_ENVIRONMENT_KEYS}
    saved_settings = (settings.ADB_HOST, settings.ADB_PORT)
    yield
    settings.ADB_HOST, settings.ADB_PORT = saved_settings
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def fake_provider_credentials(monkeypatch):
    """Configure clients with synthetic credentials and never live keys."""
    from artemis.config.settings import settings

    for field in FAKE_PROVIDER_CREDENTIAL_FIELDS:
        value = f"unit-test-{field.lower()}"
        monkeypatch.setenv(field, value)
        if hasattr(settings, field):
            monkeypatch.setattr(settings, field, SecretStr(value))


@pytest.fixture
def default_agent_config(monkeypatch, tmp_path):
    config_path = tmp_path / "artemis.jsonc"
    config_path.write_text('{"agent": {}}', encoding="utf-8")
    monkeypatch.setenv("ARTEMIS_ARTEMIS_JSONC", str(config_path))


@pytest.fixture
def fake_adb_server_factory():
    """Start fake adb servers on ephemeral loopback ports; all stop at teardown.

    Every client under test must be pointed at ``server.endpoint`` explicitly:
    the fake never replaces, and is never reachable as, the machine's adb server.
    """
    from tests.support.fake_adb import FakeAdbServer

    servers: list[FakeAdbServer] = []

    def make(name: str = "fake-adb", **kwargs) -> FakeAdbServer:
        server = FakeAdbServer(name, **kwargs).start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


@pytest.fixture
def mock_driver():
    """Provide an isolated mock mobile driver."""
    return MockDeviceDriver(device_id="fixture-mock-device", width=1080, height=2400)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Attach test-layer markers according to the owning test directory."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "e2e" in parts:
            item.add_marker(pytest.mark.e2e)
