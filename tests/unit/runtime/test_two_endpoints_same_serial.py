"""B0 acceptance: two adb endpoints, one serial, two independent runs (CHE-1094).

Two fake adb servers each expose ``emulator-5554``. Everything a run touches must
reach only its own endpoint: helper HTTP, screenshots, the UIAutomator fallback's
adb calls, device discovery, busy state and the device lock. Runs execute
concurrently; the fakes' request logs are the proof.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import shutil

from PIL import Image
import pytest

from artemis.clients.accessibility_client import AccessibilityClient
from artemis.clients.screen_client_factory import create_screen_client
from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.runtime.adb_endpoint import AdbTarget
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
from artemis.runtime.device_pool import DevicePool
from artemis.runtime.endpoint_transport import EndpointTransport
from artemis.runtime.helper_manager import AccessibilityHelperManager
from tests.unit.runtime.test_helper_stream import SERIAL, _xml, install_helper

REAL_ADB = shutil.which("adb")
needs_adb = pytest.mark.skipif(REAL_ADB is None, reason="adb binary not installed")


def _png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def two_phones(fake_adb_server_factory):
    """alpha and beta: same serial, different phone (screen colour, helper answers)."""
    phones = {}
    for name, color in (("alpha", "red"), ("beta", "blue")):
        server = fake_adb_server_factory(name)
        device = server.add_device(SERIAL)
        device.screencap = _png(color)
        install_helper(server, name)
        phones[name] = server
    return phones


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda _n: tmp_path / "locks")
    monkeypatch.setattr(
        "artemis.runtime.helper_manager.get_temp_dir", lambda _n: tmp_path / "mutex"
    )
    monkeypatch.setenv("ARTEMIS_KEEP_DEVICE_AWAKE", "false")


def _run(name: str, server, tmp_path):
    """One 'run': what a worker bound to this endpoint does for a step."""
    transport = EndpointTransport(server.endpoint, adb_path=REAL_ADB)
    manager = AccessibilityHelperManager(
        transport=transport, token_path=tmp_path / name / "token", sleep=lambda _s: None
    )
    helper = AccessibilityClient(
        SERIAL, manager=manager, transport=transport, provision_on_connect=False
    )
    helper.connect()
    xml = helper.get_hierarchy()
    helper_shot = helper.get_screenshot()
    u2_shot = UIAutomatorClient(SERIAL, transport=transport).get_screenshot()
    state = create_screen_client(SERIAL, "helper", transport=transport)
    return xml, helper_shot.getpixel((0, 0)), u2_shot.getpixel((0, 0)), type(state).__name__


@needs_adb
def test_concurrent_runs_reach_only_their_own_phone(two_phones, tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(_run, name, srv, tmp_path) for name, srv in two_phones.items()}
        results = {name: future.result(timeout=60) for name, future in futures.items()}

    assert results["alpha"][0] == _xml("alpha") and results["beta"][0] == _xml("beta")
    assert results["alpha"][1] == results["alpha"][2] == (255, 0, 0)  # red phone
    assert results["beta"][1] == results["beta"][2] == (0, 0, 255)  # blue phone
    # No adb path leaked to the other server or forwarded anything locally.
    for server in two_phones.values():
        requests = server.request_strings()
        assert not any(
            r.startswith(("host:forward", "host:kill", "host:connect")) for r in requests
        )


def test_each_endpoint_discovers_its_own_devices_and_busy_state(two_phones):
    alpha, beta = two_phones["alpha"], two_phones["beta"]
    beta.add_device("pixel-9")
    pool_a = DevicePool.for_endpoint(alpha.endpoint)
    pool_b = DevicePool.for_endpoint(beta.endpoint)
    pool_a._adb_path = pool_b._adb_path = REAL_ADB or "adb"

    lock = DeviceExecutionLock(SERIAL, "run on alpha", lock_scope=alpha.endpoint.lock_scope)
    lock.acquire()
    try:
        if REAL_ADB:
            assert [d.serial for d in pool_a.list_devices()] == [SERIAL]
            assert sorted(d.serial for d in pool_b.list_devices()) == [SERIAL, "pixel-9"]
        a_status = pool_a._build_statuses([(SERIAL, "device", None, None)])[0]
        b_status = pool_b._build_statuses([(SERIAL, "device", None, None)])[0]
    finally:
        lock.release()

    assert a_status.is_busy is True
    assert b_status.is_busy is False


def test_the_same_serial_locks_independently_per_endpoint(two_phones):
    scopes = [server.endpoint.lock_scope for server in two_phones.values()]
    first = DeviceExecutionLock(SERIAL, "alpha run", lock_scope=scopes[0])
    second = DeviceExecutionLock(SERIAL, "beta run", lock_scope=scopes[1])
    third = DeviceExecutionLock(SERIAL, "another alpha run", lock_scope=scopes[0])

    first.acquire()
    second.acquire()  # same serial, other server: not contended
    try:
        with pytest.raises(DeviceBusyError):
            third.acquire(blocking=False)
    finally:
        second.release()
        first.release()


def test_targets_of_the_same_serial_have_distinct_lock_keys(two_phones):
    keys = {AdbTarget(server.endpoint, SERIAL).lock_key for server in two_phones.values()}

    assert len(keys) == 2


@needs_adb
def test_a_process_bound_to_an_endpoint_follows_it_without_explicit_transports(
    two_phones, monkeypatch, tmp_path
):
    """The worker path: no transport argument, the process environment names the run's endpoint."""
    from artemis.config import settings

    seen = {}
    for name, server in two_phones.items():
        monkeypatch.setattr(settings, "ADB_HOST", server.endpoint.host)
        monkeypatch.setattr(settings, "ADB_PORT", server.endpoint.port)
        manager = AccessibilityHelperManager(
            token_path=tmp_path / name / "token", sleep=lambda _s: None
        )
        client = AccessibilityClient(SERIAL, manager=manager, provision_on_connect=False)
        client.connect()
        seen[name] = client.get_hierarchy()

    assert seen == {"alpha": _xml("alpha"), "beta": _xml("beta")}
