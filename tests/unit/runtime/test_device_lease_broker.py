"""Hermetic DLP-1 behavior: synthetic inventory, clocks and commit failures."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import json
import socket
import subprocess
import os

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
from broker import Broker, Device, DeviceRegistry, Refused, process_identity
from daemon import BrokerServer


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "leases.db"
        self.now = 100_000.0
        self.claim_guard = threading.RLock()
        self.registry = DeviceRegistry(
            [
                Device("a", ("usb-a", "192.0.2.1:5555"), restricted=False, identity_confirmed=True),
                Device("b", ("usb-b",), restricted=False, identity_confirmed=True),
                Device("root-a", pool="root", restricted=False, identity_confirmed=True),
            ]
        )
        self.broker = self.new_broker()

    def new_broker(self, **kwargs):
        broker = Broker(
            self.path,
            self.registry,
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            admitted={"a": "usb-a", "b": "usb-b", "root-a": "root-a"},
            **kwargs,
        )
        broker.admission_sources = {"artemis": lambda: [], "android": lambda: []}
        broker.admission_guard = lambda: self.claim_guard
        return broker

    def take(self, broker=None, **changes):
        request = dict(
            principal="agent",
            run_id="run-1",
            purpose="CHE-fixture",
            pool="rig",
            count=1,
            ttl=300,
            idempotency_key="take-1",
        )
        request.update(changes)
        return (broker or self.broker).acquire(**request)

    def refused(self, code, operation):
        with self.assertRaises(Refused) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_two_acquires_for_last_device_have_exactly_one_winner(self):
        start = threading.Barrier(2)

        def acquire(n):
            start.wait()
            try:
                return self.take(run_id=f"run-{n}", idempotency_key=f"key-{n}", device_id="a")
            except Refused as exc:
                self.assertEqual(exc.code, "device_busy")
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(acquire, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_aliases_and_batch_count_one_physical_device_once(self):
        got = self.take(count=2)
        self.assertEqual([d["device_id"] for d in got["devices"]], ["a", "b"])
        self.assertEqual(got["state"], "active")
        self.assertTrue(all("endpoint" in d and d["space_epoch"] is None for d in got["devices"]))
        busy = self.refused(
            "device_busy",
            lambda: self.take(run_id="run-2", idempotency_key="other", device_id="192.0.2.1:5555"),
        )
        self.assertEqual(busy.details["holder"]["run_id"], "run-1")

    def test_interruption_rolls_back_entire_batch_and_retry_key(self):
        def interrupt(name):
            if name == "acquire:device":
                raise KeyboardInterrupt()

        broken = self.new_broker(boundary=interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.take(broken, count=2)
        got = self.take(count=2)
        self.assertEqual(len(got["devices"]), 2)

    def test_impossible_batch_is_rejected_before_reserving_any_device(self):
        self.refused("impossible_batch", lambda: self.take(count=3))
        self.assertEqual(len(self.take(count=2)["devices"]), 2)

    def test_same_key_after_uncertain_commit_returns_same_grant(self):
        def lost_response(name):
            if name == "acquire:committed":
                raise ConnectionError("response lost")

        with self.assertRaises(ConnectionError):
            self.take(self.new_broker(boundary=lost_response), count=2)
        retry = self.take(count=2)
        self.now += 50
        self.assertEqual(self.take(count=2), retry)
        self.refused("idempotency_conflict", lambda: self.take(count=1))

    def test_one_run_cannot_hold_or_expand_a_second_lease(self):
        self.take(device_id="a")
        self.refused(
            "run_already_holds", lambda: self.take(idempotency_key="second", device_id="b")
        )

    def test_untrusted_or_ungranted_inventory_never_becomes_candidate(self):
        for device in ("500c592", "unknown"):
            with self.subTest(device=device):
                self.refused("impossible_batch", lambda: self.take(device_id=device))

    def test_ttl_count_and_key_are_validated_at_domain_boundary(self):
        for change in (
            {"ttl": 299},
            {"ttl": 3601},
            {"ttl": True},
            {"count": 0},
            {"count": True},
            {"idempotency_key": ""},
            {"run_id": ""},
        ):
            with self.subTest(change=change):
                self.refused("invalid_request", lambda: self.take(**change))

    def renew(self, group, broker=None, **changes):
        request = dict(
            lease_id=group["lease_id"],
            principal="agent",
            run_id="run-1",
            ttl=300,
            idempotency_key="renew-1",
        )
        request.update(changes)
        return (broker or self.broker).renew(**request)

    def release(self, group):
        return self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")

    def test_renew_only_in_final_third_and_never_after_expiry(self):
        got = self.take(count=2)
        self.now += 199
        self.refused("renew_too_early", lambda: self.renew(got))
        self.now += 1
        renewed = self.renew(got)
        self.assertEqual(renewed["expires_at"], self.now + 300)
        self.now = renewed["expires_at"]
        self.refused("lease_expired", lambda: self.renew(got, idempotency_key="late"))
        self.refused("device_busy", lambda: self.take(run_id="other", idempotency_key="other"))

    def test_renew_refuses_conflicting_eligible_waiter_and_draining(self):
        got = self.take(device_id="a")
        self.now += 200
        waiter = self.new_broker(conflicting_waiter=lambda conn, ids: "a" in ids)
        self.refused("conflicting_waiter", lambda: self.renew(got, waiter))
        disjoint = self.new_broker(conflicting_waiter=lambda conn, ids: "b" in ids)
        self.assertEqual(self.renew(got, disjoint)["expires_at"], self.now + 300)
        disjoint.draining = True
        self.now += 200
        self.refused("draining", lambda: self.renew(got, disjoint, idempotency_key="drain"))

    def test_renew_retry_after_lost_response_never_extends_twice(self):
        got = self.take()
        self.now += 200

        def disconnect(name):
            if name == "renew:committed":
                raise ConnectionError("lost response")

        with self.assertRaises(ConnectionError):
            self.renew(got, self.new_broker(boundary=disconnect))
        self.now += 20
        retry = self.renew(got)
        self.assertEqual(retry["expires_at"], got["expires_at"] + 200)
        self.refused("idempotency_conflict", lambda: self.renew(got, ttl=600))

    def test_hard_cap_and_release_reacquire_share_original_run_budget(self):
        got = self.take(ttl=3600)
        self.now += 2400
        got = self.renew(got, ttl=3600, idempotency_key="r1")
        self.now += 2400
        got = self.renew(got, ttl=3600, idempotency_key="r2")
        self.assertEqual(got["expires_at"], 100_000 + 7200)
        self.release(got)
        # Model the future leader-verified release, never an exposed client command.
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_groups SET state='released'")
        got = self.take(idempotency_key="reacquire", ttl=3600)
        self.assertEqual(got["started_at"], 100_000)
        self.assertEqual(got["expires_at"], 100_000 + 7200)
        self.now = got["expires_at"]
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_groups SET state='released'")
        self.refused("hard_cap", lambda: self.take(idempotency_key="cap"))

    def test_root_pool_has_30_minute_ttl_and_one_hour_cap(self):
        self.refused("invalid_request", lambda: self.take(pool="root", ttl=1801))
        got = self.take(pool="root", ttl=1800)
        self.now += 1200
        got = self.renew(got, ttl=1800, idempotency_key="r1")
        self.now += 1200
        got = self.renew(got, ttl=1800, idempotency_key="r2")
        self.assertEqual(got["expires_at"], 100_000 + 3600)

    def test_expiry_while_renew_waits_for_writer_lock_is_refused(self):
        got = self.take(count=2)
        self.now += 200
        seen = threading.Event()

        def sampled_clock():
            seen.set()
            return self.now

        self.broker.clock = sampled_clock
        with sqlite3.connect(self.path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(lambda: self.refused("lease_expired", lambda: self.renew(got)))
                try:
                    self.assertFalse(seen.wait(0.05), "clock sampled before writer lock")
                    self.now = got["expires_at"]
                finally:
                    blocker.commit()
                future.result(timeout=15)

    def test_recovery_states_keep_batch_unavailable_until_leader_verifies(self):
        got = self.take(count=2)
        released = self.release(got)
        self.assertEqual(released["state"], "fencing")
        self.assertEqual(self.release(got), released)
        for old, target in (("fencing", "cleaning"), ("cleaning", "verifying")):
            result = self.broker.recovery(got["lease_id"], old, target)
            self.assertEqual(result["state"], target)
            self.refused("device_busy", lambda: self.take(run_id="other", idempotency_key="other"))
        self.refused(
            "invalid_transition",
            lambda: self.broker.recovery(got["lease_id"], "verifying", "released"),
        )
        self.refused(
            "recovery_owner_required",
            lambda: self.broker.recovery(got["lease_id"], "verifying", "quarantine"),
        )
        result = self.broker.recovery(
            got["lease_id"],
            "verifying",
            "quarantine",
            recovery_owner="ops-fixture",
            ops_issue="CHE-fixture",
        )
        self.assertEqual(result["recovery_owner"], "ops-fixture")
        self.assertEqual(result["ops_issue"], "CHE-fixture")

    def test_renew_and_release_refuse_another_principal_or_run(self):
        got = self.take()
        for changes in ({"principal": "other"}, {"run_id": "other"}):
            with self.subTest(changes=changes):
                self.refused("lease_not_yours", lambda: self.renew(got, **changes))
                request = dict(lease_id=got["lease_id"], principal="agent", run_id="run-1")
                request.update(changes)
                self.refused("lease_not_yours", lambda: self.broker.release(**request))

    def test_daemon_validates_rpc_and_never_exposes_recovery_to_holders(self):
        server = BrokerServer(Path(self.temp.name) / "state", self.broker)
        try:
            for request in (
                {},
                [],
                {"op": "recovery"},
                {"op": "acquire", "args": {"db_path": "evil"}},
            ):
                with self.subTest(request=request):
                    self.refused("invalid_request", lambda: server.dispatch(request))
            got = server.dispatch(
                {
                    "op": "acquire",
                    "args": dict(
                        principal="agent",
                        run_id="run-1",
                        purpose="CHE-fixture",
                        pool="rig",
                        ttl=300,
                        idempotency_key="rpc",
                        binding=process_identity(os.getpid()),
                    ),
                }
            )
            status = server.dispatch(
                {
                    "op": "status",
                    "args": dict(principal="agent", run_id="run-1", lease_id=got["lease_id"]),
                }
            )
            self.assertEqual(status, got)
        finally:
            server.server_close()

    def test_private_socket_round_trip_and_single_daemon_lock(self):
        state = Path(self.temp.name) / "state"
        server = BrokerServer(state, self.broker)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            self.assertEqual((state / "broker.sock").stat().st_mode & 0o777, 0o600)
            with self.assertRaises(RuntimeError):
                BrokerServer(state, self.broker)
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(state / "broker.sock"))
                client.sendall(b'{"op":"recovery"}\n')
                response = json.loads(client.makefile("rb").readline())
                self.assertEqual(response["code"], "invalid_request")
            done = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/device-lease/device-lease"),
                    "--socket",
                    str(state / "broker.sock"),
                    "acquire",
                    "--principal",
                    "agent",
                    "--run-id",
                    "run-1",
                    "--purpose",
                    "CHE-fixture",
                    "--ttl",
                    "300",
                    "--count",
                    "2",
                    "--idempotency-key",
                    "cli",
                    "--pid",
                    str(os.getpid()),
                    "--agent-id",
                    "agent",
                    "--issue-id",
                    "CHE-fixture",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(len(json.loads(done.stdout)["devices"]), 2)
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
        self.assertFalse(worker.is_alive())
        self.assertFalse((state / "broker.sock").exists())

    def test_failed_socket_bind_preserves_error_and_releases_database_lock(self):
        state = Path(self.temp.name) / ("x" * 100)
        with self.assertRaises(OSError) as caught:
            BrokerServer(state, self.broker)
        self.assertIn("AF_UNIX path too long", str(caught.exception))
        with BrokerServer(Path(self.temp.name) / "valid", self.broker):
            pass


if __name__ == "__main__":
    unittest.main()
