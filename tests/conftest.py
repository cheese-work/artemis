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

from artemis.config.settings import settings
from artemis.drivers.mock.mock_driver import MockDeviceDriver


@pytest.fixture
def mock_driver():
    """Provide an isolated mock mobile driver."""
    return MockDeviceDriver(device_id="fixture-mock-device", width=1080, height=2400)


@pytest.fixture
def dummy_llm_keys(monkeypatch):
    """Opt-in placeholder provider keys for tests that build real LLM clients offline.

    Constructing a client (no network call) needs a key to be present. Use via
    `pytestmark = pytest.mark.usefixtures("dummy_llm_keys")`. It is deliberately not
    autouse so missing-key negative tests keep exercising the real validation.
    """
    for name in ("GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setattr(settings, name, SecretStr("test-dummy-not-a-real-key"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Attach test-layer markers according to the owning test directory."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "e2e" in parts:
            item.add_marker(pytest.mark.e2e)
