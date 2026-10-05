"""One definition of ARTEMIS_HOST_AGENT for the console and the adb transport (CHE-1094).

The registry/console (B1) and ``AdbEndpoint.create(host_id=...)`` (B0) used different
spellings, so following the ops doc enabled one and not the other.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from apps.admin_console.server import proxy_aware_app
from apps.admin_console.services import host_registry as hr
from artemis.config import host_agent
from artemis.runtime.adb_endpoint import AdbEndpoint, InvalidAdbEndpoint

ON = ["enabled", "ENABLED", " Enabled ", "1", "true", "TRUE", "yes", "on"]
OFF = ["", "0", "false", "no", "off", "disabled", "enable", "2"]


def _endpoint_creatable() -> bool:
    try:
        AdbEndpoint.create("127.0.0.1", 40000, host_id="lab-1")
    except InvalidAdbEndpoint:
        return False
    return True


def _console_enabled(admin: TestClient) -> bool:
    return bool(admin.get("/api/hosts").json()["enabled"])


@pytest.fixture
def admin():
    return TestClient(proxy_aware_app, client=("127.0.0.1", 50000), base_url="http://localhost")


@pytest.mark.parametrize("value", ON)
def test_every_enabling_spelling_turns_both_consumers_on(value, admin, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", value)

    assert host_agent.host_agent_enabled() is True
    assert hr.host_agent_enabled() is True
    assert _console_enabled(admin) is True
    assert _endpoint_creatable() is True


@pytest.mark.parametrize("value", OFF)
def test_every_other_value_leaves_both_consumers_off(value, admin, monkeypatch):
    monkeypatch.setenv("ARTEMIS_HOST_AGENT", value)

    assert host_agent.host_agent_enabled() is False
    assert hr.host_agent_enabled() is False
    assert _console_enabled(admin) is False
    assert admin.post("/api/agent/challenge", json={"host_id": "x"}).status_code == 404
    assert _endpoint_creatable() is False


def test_unset_means_off_everywhere(admin, monkeypatch):
    monkeypatch.delenv("ARTEMIS_HOST_AGENT", raising=False)

    assert hr.host_agent_enabled() is False and host_agent.host_agent_enabled() is False
    assert _console_enabled(admin) is False and _endpoint_creatable() is False


def test_the_registry_has_no_definition_of_its_own():
    """The registry's check is the shared one, not a copy that can drift again."""
    assert hr.host_agent_enabled is host_agent.host_agent_enabled
