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

import asyncio
from contextlib import contextmanager
import json
import os
import select
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _name: tmp_path,
    )


def test_device_lock_blocks_a_second_process_owner():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")

    first.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="first task"):
            second.acquire(blocking=False)
    finally:
        first.release()


def test_device_lock_recovers_stale_owner(monkeypatch):
    lock = DeviceExecutionLock("emulator-5554", "replacement task")
    lock.path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "emulator-5554",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(lambda _owner: False))

    lock.acquire()
    try:
        assert lock.path.exists()
    finally:
        lock.release()

    assert not lock.path.exists()


def test_same_serial_on_different_adb_endpoints_has_independent_locks():
    first = DeviceExecutionLock(
        "emulator-5554",
        "first endpoint",
        lock_scope="tcp:127.0.0.1:5037",
    )
    second = DeviceExecutionLock(
        "emulator-5554",
        "second endpoint",
        lock_scope="tcp:127.0.0.1:5038",
    )

    first.acquire()
    second.acquire()
    try:
        assert first.path != second.path
        owners = DeviceExecutionLock.get_active_owners()
        assert len(owners) == 2
        assert {owner.lock_scope for owner in owners.values()} == {
            "tcp:127.0.0.1:5037",
            "tcp:127.0.0.1:5038",
        }
    finally:
        second.release()
        first.release()


def test_device_lock_release_does_not_remove_replacement_owner():
    lock = DeviceExecutionLock("emulator-5554", "original task")
    lock.acquire()
    replacement = json.loads(lock.path.read_text(encoding="utf-8"))
    replacement["token"] = "replacement-token"
    lock.path.write_text(json.dumps(replacement), encoding="utf-8")

    lock.release()

    assert lock.path.exists()


def test_cleanup_stale_locks_keeps_live_owner(monkeypatch):
    live = DeviceExecutionLock("emulator-5554", "live task")
    live.acquire()
    stale_path = live.path.parent / "stale-device.lock"
    stale_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "physical-device",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    original_owner_is_alive = DeviceExecutionLock._owner_is_alive

    def owner_is_alive(owner):
        if owner.pid == 999999:
            return False
        return original_owner_is_alive(owner)

    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(owner_is_alive))
    try:
        assert DeviceExecutionLock.cleanup_stale_locks() == 1
        assert live.path.exists()
        assert not stale_path.exists()
    finally:
        live.release()


def test_device_lock_waits_and_runs_next_owner_in_fifo_order():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")
    acquired = threading.Event()

    first.acquire()

    def acquire_second():
        second.acquire()
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not acquired.is_set()

    first.release()
    assert acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


def test_submission_reservations_preserve_order_before_workers_start():
    first_ticket = DeviceExecutionLock.reserve("first submitted task")
    second_ticket = DeviceExecutionLock.reserve("second submitted task")
    second = DeviceExecutionLock("emulator-5554", "second task", second_ticket)
    second_acquired = threading.Event()

    def acquire_second():
        second.acquire()
        second_acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not second_acquired.is_set()

    first = DeviceExecutionLock("emulator-5554", "first task", first_ticket)
    first.acquire()
    first.release()

    assert second_acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


def test_pending_reservation_at_queue_head_does_not_block_other_devices():
    """An unclaimed pending submission must not gate every device's queue head.

    Regression: a reservation whose device is still "pending" used to be
    counted into every device's FIFO queue, so one unclaimed head-of-queue
    ticket blocked all devices at once. It must now park on at most one idle
    candidate device (the first in sorted order) and leave the others
    schedulable.
    """
    pending_ticket = DeviceExecutionLock.reserve("unclaimed submission")
    # device-alpha is a known idle device (a queued submission targets it), so
    # the pending ticket parks there; device-beta must stay schedulable.
    alpha_ticket = DeviceExecutionLock.reserve("task for device alpha", device_id="device-alpha")
    try:
        beta = DeviceExecutionLock("device-beta", "task for device beta")
        # Must acquire immediately even though the older pending ticket is at
        # the head of the global queue directory.
        beta.acquire(blocking=False)
        beta.release()
    finally:
        DeviceExecutionLock.cancel_reservation(pending_ticket)
        DeviceExecutionLock.cancel_reservation(alpha_ticket)


def test_pending_reservation_is_served_with_original_fifo_position_after_claim():
    """A pending reservation is not starved: once its worker claims a device it
    keeps its original submission-order position in that device's queue."""
    holder = DeviceExecutionLock("emulator-5554", "current holder")
    holder.acquire()

    # Submitted while the device is busy; the reservation stays "pending".
    first_ticket = DeviceExecutionLock.reserve("first submitted task")

    claimed = DeviceExecutionLock("emulator-5554", "claimed pending task", first_ticket)
    youngest = DeviceExecutionLock("emulator-5554", "youngest task")
    order = []
    claimed_acquired = threading.Event()
    youngest_acquired = threading.Event()

    def run_claimed():
        claimed.acquire()
        order.append("claimed")
        claimed_acquired.set()

    def run_youngest():
        youngest.acquire()
        order.append("youngest")
        youngest_acquired.set()

    t1 = threading.Thread(target=run_claimed)
    t2 = threading.Thread(target=run_youngest)
    t1.start()
    t2.start()
    time.sleep(0.2)
    assert not claimed_acquired.is_set()
    assert not youngest_acquired.is_set()

    holder.release()
    # The claimed pending ticket kept its original (oldest) FIFO timestamp and
    # must win the device before the younger concrete ticket.
    assert claimed_acquired.wait(timeout=2.0)
    assert not youngest_acquired.is_set()
    claimed.release()
    assert youngest_acquired.wait(timeout=2.0)
    youngest.release()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert order == ["claimed", "youngest"]


def test_cancelled_reservation_cannot_execute():
    ticket = DeviceExecutionLock.reserve("cancelled task")
    assert DeviceExecutionLock.cancel_reservation(ticket)

    lock = DeviceExecutionLock("emulator-5554", "cancelled task", ticket)
    with pytest.raises(DeviceBusyError, match="cancelled or expired"):
        lock.acquire()


def test_reservation_can_be_transferred_to_worker_process():
    ticket = DeviceExecutionLock.reserve("submitted task")

    assert DeviceExecutionLock.transfer_reservation(ticket, 4242, description="worker task")

    path = next((DeviceExecutionLock("device").queue_dir).glob(f"*-{ticket}.wait"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == 4242
    assert payload["description"] == "worker task"


def test_waiting_acquire_can_be_cancelled_without_leaving_a_ticket():
    owner = DeviceExecutionLock("emulator-5554", "owner")
    waiter = DeviceExecutionLock("emulator-5554", "waiter")
    cancel_event = threading.Event()
    errors = []
    owner.acquire()

    def wait_for_device():
        try:
            waiter.acquire(cancel_event=cancel_event)
        except DeviceBusyError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wait_for_device)
    thread.start()
    time.sleep(0.15)
    cancel_event.set()
    thread.join(timeout=2.0)

    try:
        assert errors == ["Waiting for the Artemis device queue was cancelled."]
        assert list(waiter.queue_dir.glob("*.wait")) == []
    finally:
        owner.release()


def test_active_owner_is_discoverable_and_can_be_annotated(monkeypatch):
    monkeypatch.setenv("ARTEMIS_TASK_INGRESS", "cli")
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    owner_lock = DeviceExecutionLock("emulator-5554", "CLI task")
    owner_lock.acquire()
    try:
        owner = DeviceExecutionLock.get_active_owner()
        assert owner is not None
        assert owner.pid == owner_lock._read_owner(owner_lock.path).pid
        assert owner.ingress == "cli"
        assert owner.session_id is None
        assert DeviceExecutionLock.is_active_owner(owner)

        assert DeviceExecutionLock.annotate_active_owner(
            session_id="cli-session",
            ingress="cli",
        )
        annotated = DeviceExecutionLock.get_active_owner()
        assert annotated is not None
        assert annotated.session_id == "cli-session"
        assert annotated.ingress == "cli"
    finally:
        owner_lock.release()


def test_multi_device_locks_can_run_concurrently():
    lock_a = DeviceExecutionLock("emulator-5554", "task on device A")
    lock_b = DeviceExecutionLock("pixel-9-test", "task on device B")

    lock_a.acquire()
    try:
        # Device B should be acquired without blocking since it's a different device
        lock_b.acquire(blocking=False)
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
            assert "emulator-5554" in active_owners
            assert "pixel-9-test" in active_owners

            # A second task on device A should be blocked
            lock_a_second = DeviceExecutionLock("emulator-5554", "second task on device A")
            with pytest.raises(DeviceBusyError, match="task on device A"):
                lock_a_second.acquire(blocking=False)
        finally:
            lock_b.release()
    finally:
        lock_a.release()


def test_global_concurrency_serial_mode_blocks_other_devices(monkeypatch):
    monkeypatch.setenv("ARTEMIS_MAX_CONCURRENT_TASKS", "1")
    lock_a = DeviceExecutionLock("device_alpha", "task alpha")
    lock_b = DeviceExecutionLock("device_beta", "task beta")

    lock_a.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="concurrency limit"):
            lock_b.acquire(blocking=False)
    finally:
        lock_a.release()

    # Once lock A is released, lock B can acquire
    lock_b.acquire(blocking=False)
    lock_b.release()


def test_owner_is_alive_guards_against_recycled_pid():
    from unittest.mock import MagicMock, patch
    from artemis.runtime.device_lock import DeviceLockOwner

    owner = DeviceLockOwner(
        pid=99999,
        process_created_at=0.0,
        token="token-1",
        device_id="dev-1",
        description="test",
        acquired_at="now",
    )

    # 1. PID is 0 or negative -> immediately dead
    invalid_owner = DeviceLockOwner(
        pid=0,
        process_created_at=0.0,
        token="token-1",
        device_id="dev-1",
        description="test",
        acquired_at="now",
    )
    assert DeviceExecutionLock._owner_is_alive(invalid_owner) is False

    # 2. Recycled PID belongs to non-Artemis system process -> treated as dead
    mock_proc = MagicMock()
    mock_proc.is_running.return_value = True
    mock_proc.name.return_value = "systemd"
    mock_proc.cmdline.return_value = ["/sbin/init"]

    with patch("psutil.Process", return_value=mock_proc):
        assert DeviceExecutionLock._owner_is_alive(owner) is False

    # 3. PID belongs to a Python / Artemis process -> alive
    mock_proc.name.return_value = "python3"
    mock_proc.cmdline.return_value = ["python3", "-m", "artemis.main"]
    with patch("psutil.Process", return_value=mock_proc):
        assert DeviceExecutionLock._owner_is_alive(owner) is True


def test_safe_unlink_and_replace_retry(tmp_path):
    f = tmp_path / "test_file.txt"
    f.write_text("hello")

    assert DeviceExecutionLock._safe_unlink(f) is True
    assert not f.exists()

    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("world")
    assert DeviceExecutionLock._safe_replace(src, dst) is True
    assert dst.read_text() == "world"


def _queue_files(tmp_path):
    return sorted((tmp_path / "artemis-global-device.queue").glob("*.wait"))


def test_queue_head_token_skips_dead_owners_and_other_scopes():
    first = DeviceExecutionLock.reserve("t", "d1", session_id="a", lock_scope="host:a")
    DeviceExecutionLock.reserve("t", "d1", session_id="b", lock_scope="host:a")
    DeviceExecutionLock.reserve("t", "d1", session_id="x", lock_scope="host:other")

    assert DeviceExecutionLock.queue_head_token("d1", "host:a") == first
    assert DeviceExecutionLock.queue_head_token("d9", "host:a") is None

    DeviceExecutionLock.cancel_reservation(first)
    head = DeviceExecutionLock.queue_head_token("d1", "host:a")
    assert head is not None and head != first


def test_queue_positions_rank_each_device_queue_in_reserve_order():
    a = [DeviceExecutionLock.reserve("t", "A") for _ in range(2)]
    b1 = DeviceExecutionLock.reserve("t", "B")
    a.append(DeviceExecutionLock.reserve("t", "A"))
    b2 = DeviceExecutionLock.reserve("t", "B")

    assert DeviceExecutionLock.queue_positions() == {
        a[0]: 1,
        a[1]: 2,
        a[2]: 3,
        b1: 1,
        b2: 2,
    }


def test_queue_positions_skip_dead_owners(tmp_path):
    dead = DeviceExecutionLock.reserve("t", "A")
    live = [DeviceExecutionLock.reserve("t", "A") for _ in range(2)]
    dead_file = _queue_files(tmp_path)[0]
    payload = json.loads(dead_file.read_text(encoding="utf-8"))
    payload["pid"] = 2**22 + 12345
    dead_file.write_text(json.dumps(payload), encoding="utf-8")

    positions = DeviceExecutionLock.queue_positions()

    assert dead not in positions
    assert positions == {live[0]: 1, live[1]: 2}


def test_queue_positions_do_not_count_the_run_that_holds_the_lock():
    head = DeviceExecutionLock.reserve("t", "A")
    rest = [DeviceExecutionLock.reserve("t", "A") for _ in range(2)]
    holder = DeviceExecutionLock("A", "t", head)
    holder.acquire(blocking=False)
    try:
        assert DeviceExecutionLock.queue_positions() == {rest[0]: 1, rest[1]: 2}
    finally:
        holder.release()


def test_queue_positions_park_pending_tickets_once():
    pending = DeviceExecutionLock.reserve("t")  # device "pending", reserved first
    on_a = DeviceExecutionLock.reserve("t", "A")

    assert DeviceExecutionLock.queue_positions() == {pending: 1, on_a: 2}

    DeviceExecutionLock.cancel_reservation(on_a)
    assert DeviceExecutionLock.queue_positions() == {}  # nothing to park on


def test_owner_lock_waits_for_shared_admission_guard(tmp_path, monkeypatch):
    """CHE-5297: the DLP-1 broker and Artemis lock writers share one exclusion."""
    import fcntl

    from pathlib import Path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools/device-lease"))
    from broker import Broker, Device, DeviceRegistry
    from compose import FileGuard
    from device_lease.claims import ClaimLedger

    guard = tmp_path / "claims.guard"
    database = tmp_path / "leases.sqlite3"
    registry = DeviceRegistry([Device("emulator-5554", restricted=False, identity_confirmed=True)])
    Broker(database, registry).inventory()
    ledger = ClaimLedger(database, FileGuard(guard), registry, tmp_path)
    ledger.census(
        generation=1,
        observed_at=time.time(),
        complete=True,
        writers=["android", "artemis"],
        claims=[],
    )
    monkeypatch.setenv("ARTEMIS_DEVICE_ADMISSION_GUARD", str(guard))
    monkeypatch.setenv("ARTEMIS_DEVICE_CLAIMS_DB", str(database))
    holder = os.open(guard, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    lock = DeviceExecutionLock("emulator-5554", "guarded task")
    worker = threading.Thread(target=lock.acquire)
    worker.start()
    try:
        time.sleep(0.3)
        assert not lock.path.exists()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    worker.join(timeout=5)
    assert lock.path.exists()
    lock.release()


def test_held_admission_guard_respects_acquire_timeout(tmp_path, monkeypatch):
    import fcntl
    from concurrent.futures import ThreadPoolExecutor

    guard = tmp_path / "claims.guard"
    monkeypatch.setenv("ARTEMIS_DEVICE_ADMISSION_GUARD", str(guard))
    monkeypatch.setenv("ARTEMIS_DEVICE_CLAIMS_DB", str(tmp_path / "leases.sqlite3"))
    lock = DeviceExecutionLock("emulator-5554")
    with ThreadPoolExecutor() as workers, guard.open("wb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        future = workers.submit(lock.acquire, timeout=0.05, poll_interval=0.01)
        with pytest.raises(DeviceBusyError, match="Timed out waiting for the Artemis device queue"):
            future.result(timeout=1)
    assert not lock.path.exists()
    assert not list(lock.queue_dir.glob("*.wait"))


def test_guarded_webusb_lock_uses_enrolled_identity_across_ports(tmp_path, monkeypatch):
    import sqlite3

    guard = tmp_path / "claims.guard"
    database = tmp_path / "leases.sqlite3"
    alias = "webusb:" + "a" * 64
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE android_claim_source (singleton, generation, complete, observed_at, "
            "max_age, aliases, guard_path, lock_dir);"
            "CREATE TABLE broker_devices (device_id, lease_id);"
            "CREATE TABLE broker_groups (lease_id, state);"
            "CREATE TABLE broker_tickets (state, reserved);"
            "CREATE TABLE android_claims (device_id);"
        )
        connection.execute(
            "INSERT INTO android_claim_source VALUES (1, 1, 1, unixepoch(), 60, ?, ?, ?)",
            (
                json.dumps({alias: "relay-phone", "relay-phone": "relay-phone"}),
                str(guard),
                str(tmp_path),
            ),
        )
    monkeypatch.setenv("ARTEMIS_DEVICE_ADMISSION_GUARD", str(guard))
    monkeypatch.setenv("ARTEMIS_DEVICE_CLAIMS_DB", str(database))
    monkeypatch.setenv(DeviceExecutionLock.WEBUSB_CLAIM_ALIAS_ENV, alias)
    monkeypatch.setenv("ARTEMIS_TASK_INGRESS", "frontend")

    first = DeviceExecutionLock("127.0.0.1:44903", ingress="frontend", session_id="browser-run")
    second = DeviceExecutionLock("127.0.0.1:44904", ingress="frontend", session_id="next-run")
    unknown = DeviceExecutionLock("127.0.0.1:44905", webusb_claim_alias="webusb:" + "b" * 64)
    inferred_frontend = DeviceExecutionLock("127.0.0.1:44906", session_id="sdk-run")
    with pytest.raises(DeviceBusyError, match="not registered with the device broker"):
        inferred_frontend._try_acquire_owner_lock()

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO android_claims VALUES ('relay-phone')")
    assert first._try_acquire_owner_lock() is False
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM android_claims")
    try:
        assert first._try_acquire_owner_lock() is True
        assert DeviceExecutionLock.annotate_active_owner(session_id="relay-run")
        assert json.loads(first.path.read_text())["device_id"] == "127.0.0.1:44903"
        assert second._try_acquire_owner_lock() is False
        with pytest.raises(DeviceBusyError, match="not registered with the device broker"):
            unknown._try_acquire_owner_lock()
    finally:
        first.release()

    try:
        assert second._try_acquire_owner_lock() is True
    finally:
        second.release()


@pytest.fixture
def broker_handoff(tmp_path, monkeypatch):
    guard = tmp_path / "claims.guard"
    guard.touch()
    database = tmp_path / "leases.sqlite3"
    database.touch()
    for name, value in {
        "ARTEMIS_DEVICE_ADMISSION_GUARD": str(guard),
        "ARTEMIS_DEVICE_CLAIMS_DB": str(database),
        "MULTICA_AGENT_ID": "fixture-agent",
        "MULTICA_TASK_ID": "fixture-run",
        "MULTICA_RUN_BINDING_ID": "fixture-binding",
        "ARTEMIS_DEVICE_LEASE_HANDOFF": json.dumps(
            {
                "socket": str(tmp_path / "broker.sock"),
                "lease_id": "fixture-lease",
                "device_id": "fixture-device",
                "issue_id": "fixture-issue",
                "generation": 1,
                "phase": "work",
            }
        ),
    }.items():
        monkeypatch.setenv(name, value)
    broker_rpc = Mock(return_value={"acquired": True})
    readback = Mock()
    monkeypatch.setattr("device_lease.transport.rpc", broker_rpc)
    monkeypatch.setattr("device_lease.claims.check_sdk_handoff", readback)
    return guard, broker_rpc, readback


def test_handoff_readback_guard_respects_acquire_timeout(broker_handoff):
    import fcntl
    from concurrent.futures import ThreadPoolExecutor

    guard, broker_rpc, readback = broker_handoff
    lock = DeviceExecutionLock("emulator-5554")
    with ThreadPoolExecutor() as workers, guard.open("wb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        future = workers.submit(lock.acquire, timeout=0.05, poll_interval=0.01)
        with pytest.raises(DeviceBusyError, match="Timed out waiting for the Artemis device queue"):
            future.result(timeout=1)

    assert broker_rpc.called
    readback.assert_not_called()
    assert not lock.path.exists()
    assert not list(lock.queue_dir.glob("*.wait"))


@contextmanager
def _brief_broker_guard_hold(guard):
    script = """\
import fcntl
import sys
import time

with open(sys.argv[1], 'rb') as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    print('ready', flush=True)
    if sys.stdin.read(1):
        time.sleep(0.5)
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(guard)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            assert select.select([holder.stdout], [], [], 5)[0], "guard holder did not start"
            assert holder.stdout.readline() == "ready\n"
            yield holder.stdin
        finally:
            holder.stdin.close()
            try:
                assert holder.wait(timeout=5) == 0
            except subprocess.TimeoutExpired:
                holder.kill()
                raise


@pytest.fixture(params=["validation", "sdk-init"])
def managed_avd_operation(request, broker_handoff, monkeypatch):
    _, broker_rpc, readback = broker_handoff
    if request.param == "validation":
        lock = DeviceExecutionLock("emulator-5554")
        lock.acquire(blocking=False)
        broker_rpc.reset_mock()
        readback.reset_mock()
        try:
            yield request.param, lock.validate_handoff
        finally:
            lock.release()
        return

    from artemis.sdk.agent import Agent, DevicePlatform

    monkeypatch.delenv("ARTEMIS_CLOUD_MODE", raising=False)
    monkeypatch.delenv("ARTEMIS_CHAT_SESSION_ID", raising=False)
    agent = Agent.__new__(Agent)
    agent._initialized = False
    agent._session_id = "fixture"
    agent._lease_execution_lock = None
    agent._config = SimpleNamespace(
        device_id="emulator-5554",
        device_platform=DevicePlatform.ANDROID,
        servers=SimpleNamespace(adb_host="127.0.0.1", adb_port=5037),
        concurrency_mode="per_device",
        max_concurrency=None,
    )
    monkeypatch.setattr(agent, "_init_device", AsyncMock(return_value=True))
    try:
        yield request.param, lambda: asyncio.run(agent._init_internal())
    finally:
        if agent._lease_execution_lock is not None:
            agent._lease_execution_lock.release()


def test_managed_avd_retries_brief_guard_contention(broker_handoff, managed_avd_operation):
    guard, broker_rpc, readback = broker_handoff
    _, operation = managed_avd_operation
    with _brief_broker_guard_hold(guard) as release:

        def acquire_from_broker(*args):
            if broker_rpc.call_count == 1:
                release.write("1")
                release.flush()
            return {"acquired": True}

        broker_rpc.side_effect = acquire_from_broker
        assert operation() is True

    broker_rpc.assert_called_once()
    readback.assert_called_once()


def test_managed_avd_reports_broker_admission_timeout(broker_handoff, managed_avd_operation):
    import fcntl

    guard, broker_rpc, readback = broker_handoff
    _, operation = managed_avd_operation
    with guard.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        started_at = time.monotonic()
        with pytest.raises(
            DeviceBusyError, match=r"Timed out waiting for device broker admission after 2\.0s"
        ):
            operation()
        assert 1.9 <= time.monotonic() - started_at < 4

    broker_rpc.assert_called_once()
    readback.assert_not_called()
    assert not _queue_files(guard.parent)


def test_managed_avd_does_not_retry_occupied_device(
    broker_handoff, managed_avd_operation, monkeypatch
):
    _, broker_rpc, readback = broker_handoff
    operation_name, operation = managed_avd_operation
    broker_rpc.return_value = {"code": "execution_lock_busy"}
    sleep = Mock(side_effect=AssertionError("occupied device must not wait"))
    monkeypatch.setattr("artemis.runtime.device_lock.time.sleep", sleep)

    if operation_name == "validation":
        assert operation() is False
    else:
        with pytest.raises(DeviceBusyError, match="already controlled"):
            operation()

    broker_rpc.assert_called_once()
    readback.assert_not_called()
    sleep.assert_not_called()
