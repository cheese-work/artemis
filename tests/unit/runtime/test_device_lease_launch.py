"""Lease launch tests: synthetic processes/ADB only, never a live emulator."""

from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_startup as fixtures
from broker import DeviceRegistry, Refused
from daemon import BrokerServer, ControlServer
from owned_recovery import OwnedRecovery
import startup
from authority import Authority


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

    def check_binding(self, device, journal, identity, *, work):
        if journal["worker"] != self.worker or journal["child"] != self.child:
            raise startup.StartupMismatch("synthetic process replacement")


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

    def owner(self):
        return dict(lease_id=self.group["lease_id"], principal="agent", run_id="run-1")

    def recover_after_restart(self, live):
        old = self.broker
        recovery = OwnedRecovery(self.path, registry=self.registry, monotonic=lambda: self.now)
        self.broker = self.new_broker(
            managed_runs=old.managed_runs,
            startup_snapshot=self.read_proof,
            startup_io=self.io,
            recovery_io=recovery,
            recovery_owner="ops-fixture",
            ops_issue="CHE-recovery",
        )
        stopped = []

        def stop(identity, timeout):
            stopped.append(live.pop(identity["pid"]))
            return True

        with (
            patch.object(recovery, "_identity", side_effect=live.get),
            patch.object(recovery, "_descendants", return_value=set()),
            patch.object(recovery, "_daemon_pid", return_value=7000),
            patch.object(recovery, "_stop", side_effect=stop),
            patch.object(
                recovery, "_adb", side_effect=AssertionError("AVD cleanup must not target ADB")
            ),
            patch.object(startup.LinuxStartupIO, "resource_users", return_value=set()),
            patch.object(startup.LinuxStartupIO, "socket_owners", return_value={5037: {7000}}),
            BrokerServer(Path(self.temp.name) / "socket", self.broker) as server,
        ):
            server.service_actions()
            server.service_actions()
        return stopped

    def test_restart_after_execution_stops_only_journalled_processes_and_requires_verifier(self):
        data = Path(self.temp.name) / "existing.avd"
        data.mkdir()
        userdata = data / "userdata-qemu.img"
        userdata.write_bytes(b"pre-existing data")
        self.device = replace(self.device, avd_data_path=str(data))
        self.registry = self.broker.registry = DeviceRegistry([self.device])
        self.proof["device"] = asdict(self.device)
        self.publish()
        self.group = self.start()
        self.io.error = ConnectionError("crash after execution")
        with self.assertRaises(ConnectionError):
            self.launch()
        unrelated = dict(self.io.child, pid=9000)
        live = {row["pid"]: row for row in (self.io.worker, self.io.child, unrelated)}
        stopped = self.recover_after_restart(live)
        self.assertEqual(stopped, [self.io.child, self.io.worker])
        self.assertEqual(live, {9000: unrelated})
        self.assertEqual(userdata.read_bytes(), b"pre-existing data")
        self.assertEqual(self.io.calls.count("execute"), 1)
        status = self.broker.status(**self.owner())
        self.assertEqual(status["state"], "verifying")
        for field in ("expires_at", "hard_deadline"):
            self.assertEqual(status[field], self.group[field])
        self.refused("lease_not_active", self.launch)
        self.refused("lease_not_active", self.start)
        self.refused("run_already_holds", lambda: self.start(idempotency_key="new"))
        self.refused(
            "leader_required",
            lambda: self.broker.verify_release(
                self.group["lease_id"], verifier="agent", evidence="cleanup", passed=True
            ),
        )
        result = self.broker.verify_release(
            self.group["lease_id"],
            verifier=self.broker.leader_id,
            evidence="independent",
            passed=True,
        )
        self.assertEqual(result["state"], "released")
        self.assertEqual(self.start(idempotency_key="new")["state"], "active")

    def test_restart_with_reused_pid_quarantines_without_signalling_either_process(self):
        self.group = self.start()
        self.launch()
        replacement = dict(self.io.child, process_start="reused")
        live = {row["pid"]: row for row in (self.io.worker, replacement)}
        self.assertEqual(self.recover_after_restart(live), [])
        self.assertEqual(live[replacement["pid"]], replacement)
        result = self.broker.status(**self.owner())
        self.assertEqual(result["state"], "quarantine")
        self.assertEqual(result["recovery_owner"], "ops-fixture")
        self.refused(
            "state_conflict",
            lambda: self.broker.verify_release(
                self.group["lease_id"],
                verifier=self.broker.leader_id,
                evidence="absence",
                passed=True,
            ),
        )

    def test_scope_drift_and_worker_replacement_fence_before_work_mutations(self):
        self.group = self.start()
        self.launch()
        self.verify()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_access SET phase='work'")
        self.io.worker = dict(self.io.worker, process_start="replacement")
        self.refused(
            "lease_not_active",
            lambda: self.broker.record_change(
                **self.owner(),
                change=dict(kind="home", device_id=self.device.device_id, allowed=True),
            ),
        )
        self.assertEqual(self.broker.status(**self.owner())["state"], "fencing")

    def test_missing_startup_work_manifest_holds_capacity_without_signalling(self):
        self.group = self.start()
        self.launch()
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM broker_changes")
        live = {row["pid"]: row for row in (self.io.worker, self.io.child)}
        self.assertEqual(self.recover_after_restart(live), [])
        self.assertEqual(len(live), 2)
        self.assertEqual(self.broker.status(**self.owner())["state"], "quarantine")
        self.refused("run_already_holds", lambda: self.start(idempotency_key="new"))

    def test_revoked_startup_snapshot_fences_and_does_not_replay_active_lease(self):
        self.group = self.start()
        self.proof["grant"]["reference"] = "replacement"
        self.publish()
        self.assertEqual(self.broker.status(**self.owner())["state"], "fencing")
        self.refused("lease_not_active", self.start)

    def test_expiry_after_permission_commit_keeps_execution_gate_closed(self):
        self.group = self.start()

        def boundary(name):
            if name == "launch:executing":
                self.now = self.group["expires_at"]

        self.broker.boundary = boundary
        self.refused("lease_not_active", self.launch)
        self.assertNotIn("execute", self.io.calls)
        self.assertIn("abort", self.io.calls)

    def test_expiry_revoke_lost_holder_and_authority_change_fence_both_phases(self):
        managed = self.broker.managed_runs
        for phase in ("starting", "work"):
            for fault in ("expiry", "revoke", "lost-holder", "authority-change"):
                with self.subTest(phase=phase, fault=fault):
                    self.now = self.proof["grant"]["observed_at"]
                    directory = Path(self.temp.name) / f"{phase}-{fault}"
                    directory.mkdir()
                    self.path = directory / "leases.db"
                    self.broker = self.new_broker(
                        managed_runs=managed, startup_snapshot=self.read_proof, startup_io=self.io
                    )
                    self.write_record()
                    self.group = self.start()
                    self.launch()
                    if phase == "work":
                        self.verify()
                        with sqlite3.connect(self.path) as conn:
                            conn.execute("UPDATE broker_access SET phase='work'")
                    if fault == "expiry":
                        self.now = self.group["expires_at"]
                    elif fault == "revoke":
                        self.broker.revoke(self.group["lease_id"], actor="ops", role="ops")
                    elif fault == "lost-holder":
                        self.record_path.unlink()
                    else:
                        authority = Authority(
                            self.broker, actor="ops", run_id="control", issue="CHE-control"
                        )
                        authority.drain("broker", 1)
                        authority.fence("broker", 1, "next")
                    status = self.broker.status(**self.owner())
                    self.assertEqual(status["state"], "fencing")
                    self.assertEqual(status["expires_at"], self.group["expires_at"])
                    self.assertEqual(status["hard_deadline"], self.group["hard_deadline"])
                    self.refused(
                        "lease_not_active",
                        lambda: self.broker.record_change(
                            **self.owner(),
                            change=dict(kind="home", device_id=self.device.device_id, allowed=True),
                        ),
                    )

    def test_crash_boundaries_preserve_committed_occupancy_and_prevent_relaunch(self):
        def interrupt(name):
            if name == boundary:
                raise KeyboardInterrupt()

        managed = self.broker.managed_runs
        for boundary, state in (
            ("acquire:device", None),
            ("startup:committed", "verifying"),
            ("launch:planned", "quarantine"),
            ("launch:prepared", "quarantine"),
            ("launch:journalled", "verifying"),
            ("launch:executing", "verifying"),
        ):
            with self.subTest(boundary=boundary):
                directory = Path(self.temp.name) / boundary.replace(":", "-")
                directory.mkdir()
                self.path = directory / "leases.db"
                self.broker = self.new_broker(
                    managed_runs=managed, startup_snapshot=self.read_proof, startup_io=self.io
                )
                self.io.calls.clear()
                self.broker.boundary = interrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.group = self.start()
                    self.launch()
                with sqlite3.connect(self.path) as conn:
                    row = conn.execute("SELECT lease_id FROM broker_groups").fetchone()
                self.assertNotIn("execute", self.io.calls)
                self.assertEqual(self.recover_after_restart({}), [])
                if state is None:
                    self.assertIsNone(row)
                    continue
                self.group = {"lease_id": row[0]}
                self.assertEqual(self.broker.status(**self.owner())["state"], state)
                self.refused("lease_not_active", self.launch)

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
        self.refused("lease_not_active", self.launch)
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

    def assert_retry_keeps_starting_lease(self):
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT state,phase,expires_at,hard_deadline FROM broker_groups "
                    "JOIN broker_access USING (lease_id)"
                ).fetchone(),
                ("active", "starting", self.group["expires_at"], self.group["hard_deadline"]),
            )
            self.assertIsNone(conn.execute("SELECT identity FROM broker_launches").fetchone()[0])

    def test_offline_serial_then_device_retries_same_journal_without_fencing(self):
        self.group = self.start()
        self.launch()
        evidence = {
            key: value
            for key, value in self.io.observation.items()
            if key not in {"avd_name", "api", "abi", "boot_complete", "serial", "device_state"}
        }
        adapter = startup.LinuxStartupIO(lambda: None)
        self.broker.startup_io = adapter
        guest = {
            key: self.io.observation[key] for key in ("avd_name", "api", "abi", "boot_complete")
        }
        with (
            patch.object(adapter, "process_evidence", return_value=evidence),
            patch.object(adapter, "_process_evidence", return_value=evidence),
            patch(
                "startup.process_identity",
                side_effect={8001: self.io.worker, 8002: self.io.child}.get,
            ),
            patch(
                "startup.devices",
                side_effect=[
                    {self.device.aliases[0]: "offline"},
                    {self.device.aliases[0]: "device"},
                ],
            ),
            patch("startup.guest_identity", return_value=guest) as probe,
        ):
            self.refused("startup_not_ready", self.verify)
            probe.assert_not_called()
            self.assert_retry_keeps_starting_lease()
            self.assertEqual(self.verify()["identity"], self.io.observation)
        self.assertEqual(self.io.calls.count("prepare"), 1)

    def test_boot_incomplete_and_observation_errors_are_retryable(self):
        self.group = self.start()
        self.launch()
        original = copy.deepcopy(self.io.observation)
        for field, value in (
            ("boot_complete", ""),
            ("boot_complete", "0"),
            ("device_state", "offline"),
        ):
            with self.subTest(field=field, value=value):
                self.io.observation = dict(original, **{field: value})
                self.refused("startup_not_ready", self.verify)
                self.assert_retry_keeps_starting_lease()
        for error in (
            TimeoutError("ADB timeout"),
            ConnectionError("ADB lost"),
            PermissionError("proc unavailable"),
            ValueError("malformed ADB reply"),
        ):
            with self.subTest(error=error), patch.object(self.io, "observe", side_effect=error):
                self.refused("startup_observation_unavailable", self.verify)
                self.assert_retry_keeps_starting_lease()
        self.io.observation = original
        self.assertEqual(self.verify()["identity"], original)
        self.assertEqual(self.io.calls.count("prepare"), 1)

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
