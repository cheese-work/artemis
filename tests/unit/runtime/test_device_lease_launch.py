"""Lease launch tests: synthetic processes/ADB only, never a live emulator."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_startup as fixtures
from broker import Refused
from daemon import BrokerServer, ControlServer


class FakeLifecycle:
    def __init__(self, case):
        self.case = case
        self.calls = []
        self.available = True
        self.busy = False
        self.error = None
        self.worker = dict(mode="local", pid=8001, process_start="11", boot_id="boot")
        self.child = dict(mode="local", pid=8002, process_start="12", boot_id="boot")
        self.observation = dict(
            worker=self.worker,
            child=self.child,
            parent_pid=8001,
            avd_name=case.device.avd_name,
            data_path=case.device.avd_data_path,
            userdata=case.device.avd_data_path + "/userdata-qemu.img",
            console_port=case.device.console_port,
            adb_port=case.device.adb_port,
            serial=case.device.aliases[0],
            api=case.device.api,
            abi=case.device.abi,
            socket_pids=[8002],
            userdata_pids=[8002],
            device_state="device",
            boot_complete="1",
        )

    def preflight(self, device, journal=None):
        self.calls.append("preflight")
        if not self.available or self.busy:
            raise ValueError("shared listener unknown or resource busy")

    def prepare(self, device):
        # A fresh connection sees the committed attempt before ANY process is spawned.
        with sqlite3.connect(self.case.path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM broker_launches").fetchone()
        self.case.assertEqual(row, ("planned",))
        self.calls.append("prepare")
        return {"worker": self.worker, "child": self.child}

    def resume(self, handle):
        with sqlite3.connect(self.case.path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            state, evidence = conn.execute("SELECT state,journal FROM broker_launches").fetchone()
        self.case.assertEqual(state, "executing")
        self.case.assertEqual(json.loads(evidence), handle)
        self.calls.append("execute")
        if self.error:
            raise self.error

    def abort(self, handle):
        self.calls.append("abort")

    def observe(self, device, journal):
        self.calls.append("observe")
        return copy.deepcopy(self.observation)


class LaunchTests(unittest.TestCase):
    setUpFixtures = fixtures.StartupTests.setUpFixtures
    new_broker = fixtures.StartupTests.new_broker
    write_record = fixtures.StartupTests.write_record
    refused = fixtures.StartupTests.refused
    publish = fixtures.StartupTests.publish
    read_proof = fixtures.StartupTests.read_proof
    start = fixtures.StartupTests.start

    def setUp(self):
        fixtures.StartupTests.setUp(self)
        self.io = FakeLifecycle(self)
        self.broker.startup_io = self.io

    def launch(self, group=None):
        group = group or self.group
        server = ControlServer.__new__(ControlServer)
        server.broker = self.broker
        return server.dispatch(
            {
                "op": "launch-startup",
                "args": dict(lease_id=group["lease_id"], principal="agent", run_id="run-1"),
            }
        )

    def verify(self):
        return self.broker.verify_startup(
            lease_id=self.group["lease_id"], principal="agent", run_id="run-1"
        )

    def test_journal_precedes_execution_and_retry_never_spawns_twice_or_renews(self):
        self.group = self.start()
        first = self.launch()
        self.now += 20
        self.assertEqual(first, self.launch())
        self.assertEqual(self.io.calls.count("prepare"), 1)
        self.assertEqual(self.io.calls.count("execute"), 1)
        got = self.verify()
        self.assertEqual(got["identity"]["serial"], self.device.aliases[0])
        status = self.broker.status(
            lease_id=self.group["lease_id"], principal="agent", run_id="run-1"
        )
        self.assertEqual(status["access_phase"], "starting")
        self.assertEqual(status["expires_at"], self.group["expires_at"])
        self.assertEqual(status["hard_deadline"], self.group["hard_deadline"])

    def test_failed_journal_never_executes_and_attempt_is_not_retried(self):
        self.group = self.start()

        def boundary(name):
            if name == "launch:prepared":
                raise OSError("journal unavailable")

        self.broker.boundary = boundary
        with self.assertRaises(OSError):
            self.launch()
        self.assertNotIn("execute", self.io.calls)
        self.assertIn("abort", self.io.calls)
        self.broker.boundary = lambda _: None
        self.refused("startup_launch_uncertain", self.launch)
        self.assertEqual(self.io.calls.count("prepare"), 1)

    def test_sqlite_journal_failure_never_executes(self):
        self.group = self.start()
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TRIGGER fail_launch_journal BEFORE UPDATE OF journal ON broker_launches "
                "BEGIN SELECT RAISE(ABORT, 'journal write failed'); END"
            )
        with self.assertRaises(sqlite3.DatabaseError):
            self.launch()
        self.assertNotIn("execute", self.io.calls)
        self.assertIn("abort", self.io.calls)
        self.refused("startup_launch_uncertain", self.launch)

    def test_consumed_reference_cannot_launch_a_second_lease(self):
        self.group = self.start()
        self.launch()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_groups SET state='released'")
        self.group = self.start(idempotency_key="second")
        self.refused("startup_grant_consumed", self.launch)
        self.assertEqual(self.io.calls.count("execute"), 1)

    def test_uncertain_execute_response_and_racing_requests_cannot_reexecute(self):
        self.group = self.start()
        self.io.error = ConnectionError("lost acknowledgement")
        with self.assertRaises(ConnectionError):
            self.launch()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.launch(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.io.calls.count("execute"), 1)

    def test_missing_shared_listener_and_busy_resources_block_before_spawn(self):
        self.group = self.start()
        for field in ("available", "busy"):
            setattr(self.io, field, field == "busy")
            self.refused("startup_preflight_failed", self.launch)
            setattr(self.io, field, field != "busy")
        self.assertNotIn("prepare", self.io.calls)
        self.assertNotIn("execute", self.io.calls)

    def test_changed_or_missing_authority_and_foreign_holder_cannot_launch(self):
        self.group = self.start()
        self.proof["run_id"] = "other"
        self.publish()
        self.refused("startup_not_admitted", self.launch)
        self.assertEqual(self.io.calls, [])
        self.proof["run_id"] = "run-1"
        self.publish()
        self.refused(
            "lease_not_yours",
            lambda: self.broker.launch_startup(
                lease_id=self.group["lease_id"], principal="other", run_id="other"
            ),
        )

    def test_each_process_and_guest_mismatch_refuses_revalidation_and_fences(self):
        self.group = self.start()
        self.launch()
        original = copy.deepcopy(self.io.observation)
        variants = [
            (key, value)
            for key, value in (
                ("avd_name", "wrong"),
                ("data_path", "/wrong"),
                ("userdata", "/wrong"),
                ("console_port", 5586),
                ("adb_port", 5587),
                ("serial", "emulator-5586"),
                ("api", 34),
                ("abi", "arm64-v8a"),
                ("parent_pid", 999),
                ("socket_pids", [8002, 9000]),
                ("userdata_pids", [9000]),
            )
        ]
        for key in ("worker", "child"):
            for field, value in (("pid", 999), ("process_start", "new"), ("boot_id", "new")):
                row = dict(original[key], **{field: value})
                variants.append((key, row))
        for key, value in variants:
            with self.subTest(key=key, value=value):
                with sqlite3.connect(self.path) as conn:
                    conn.execute("UPDATE broker_groups SET state='active'")
                self.io.observation = dict(copy.deepcopy(original), **{key: value})
                self.refused("startup_identity_mismatch", self.verify)
                with sqlite3.connect(self.path) as conn:
                    self.assertEqual(
                        conn.execute("SELECT state FROM broker_groups").fetchone()[0], "fencing"
                    )

    def test_no_probe_before_binding_and_device_observation_and_no_holder_rpc(self):
        self.group = self.start()
        self.refused("startup_not_launched", self.verify)
        self.assertNotIn("observe", self.io.calls)
        server = BrokerServer.__new__(BrokerServer)
        server.broker = self.broker
        self.refused(
            "invalid_request", lambda: server.dispatch({"op": "launch-startup", "args": {}})
        )

    def test_foreign_run_proof_cannot_offer_startup_ticket(self):
        self.group = self.start()
        self.record.update(task_id="run-2", issue_id="CHE-queued")
        self.write_record()
        self.proof.update(run_id="run-2", issue_id="CHE-queued")
        self.publish()
        queued = self.refused(
            "device_busy",
            lambda: self.start(run_id="run-2", issue_id="CHE-queued", idempotency_key="queued"),
        )
        ticket = queued.details["ticket"]
        # Freeing occupancy here is a fixture setup, not a lifecycle operation.
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_groups SET state='released'")
        self.proof["run_id"] = "foreign"
        self.publish()
        got = self.broker.ticket_status(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-queued",
        )
        self.assertEqual(got["state"], "queued")


if __name__ == "__main__":
    unittest.main()
