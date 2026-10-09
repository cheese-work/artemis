import threading

import pytest

from artemis.runtime import awake_service
from tests.support.awake_service_isolation import stop_awake_service


@pytest.fixture(params=["registered", "unregistered-by-stop-device"])
def heartbeat_in_adb_call(request, monkeypatch):
    """A real heartbeat thread blocked inside its adb call, past the service's 2s join."""
    entered, release = threading.Event(), threading.Event()

    def blocked_adb_call(*_args, **_kwargs):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(awake_service, "AWAKE_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(awake_service, "_run_awake_adb_command", blocked_adb_call)
    awake_service.screen_awake_service._start_heartbeat("test-key", "test-device", None)
    assert entered.wait(5)
    if request.param != "registered":
        awake_service.screen_awake_service._stop_device("test-key")
        assert not awake_service.screen_awake_service._heartbeat_threads
    try:
        yield release
    finally:
        release.set()
        stop_awake_service()


def test_stop_waits_for_a_heartbeat_inside_an_adb_call(heartbeat_in_adb_call):
    threading.Timer(2.5, heartbeat_in_adb_call.set).start()
    stop_awake_service()
    assert not any(
        thread.name.startswith("artemis-awake-heartbeat") for thread in threading.enumerate()
    )


def test_stop_fails_instead_of_returning_while_a_heartbeat_is_alive(heartbeat_in_adb_call):
    with pytest.raises(RuntimeError, match="still running"):
        stop_awake_service(timeout=0.05)
