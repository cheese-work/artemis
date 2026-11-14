"""Recovery policy with fixture device I/O and disposable local process checks."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_broker as fixtures

IDENTITY = {"mode": "local", "pid": 1234, "process_start": "42", "boot_id": "fixture-boot"}


class OwnedRecoveryTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker

    def take(self, **changes):
        if "binding" not in changes:
            probe = patch.object(self.broker, "run_probe", return_value=True)
            probe.start()
            self.addCleanup(probe.stop)
            changes["binding"] = IDENTITY
        return fixtures.BrokerTests.take(self, **changes)

    def worker(self):
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def cleanup():
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=5)

        self.addCleanup(cleanup)
        return worker, fixtures.process_identity(worker.pid)

    def work(self, processes=None, forwards=None):
        return {
            "kind": "work",
            "device_id": "a",
            "complete": True,
            "processes": [IDENTITY] if processes is None else processes,
            "forwards": forwards or [],
        }

    def record(self, group, change):
        self.broker.record_change(
            lease_id=group["lease_id"], principal="agent", run_id="run-1", change=change
        )

    def test_work_tracking_journal_is_recorded_before_execution(self):
        group = self.take(device_id="a")
        self.record(group, self.work())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                json.loads(conn.execute("SELECT change FROM broker_changes").fetchone()[0]),
                self.work(),
            )

    def adapter(self):
        from owned_recovery import OwnedRecovery

        return OwnedRecovery(self.path)

    def test_adb_empty_listing_newline_is_empty_but_malformed_row_is_refused(self):
        adapter, group = self.adapter(), self.take(device_id="a")
        for direction in ("forward", "reverse"):
            with self.subTest(direction=direction):
                with patch.object(adapter, "_adb", return_value="\n"):
                    self.assertEqual(adapter._forwards(group, "a", direction, 2), {})
                with patch.object(adapter, "_adb", return_value="malformed\n"):
                    with self.assertRaises(ValueError):
                        adapter._forwards(group, "a", direction, 2)

    def group(self, tracking=True):
        group = self.take(device_id="a")
        if tracking:
            self.record(group, self.work())
        return group

    def test_missing_tracking_unknown_work_pid_reuse_and_daemon_never_signal(self):
        adapter = self.adapter()
        group = self.group(False)
        with patch.object(adapter, "_stop") as stop:
            self.assertFalse(adapter.fence(group, timeout=2))
            stop.assert_not_called()
        self.record(group, self.work())
        for identities, descendants, daemon in [
            ({1234: dict(IDENTITY, process_start="other")}, set(), 999),
            ({1234: IDENTITY}, {8888}, 999),
            ({1234: IDENTITY}, set(), 1234),
        ]:
            with (
                patch.object(adapter, "_identity", side_effect=lambda pid: identities.get(pid)),
                patch.object(adapter, "_descendants", return_value=descendants),
                patch.object(adapter, "_daemon_pid", return_value=daemon),
                patch.object(adapter, "_stop") as stop,
            ):
                self.assertFalse(adapter.fence(group, timeout=2))
                stop.assert_not_called()

    def test_exact_process_and_forward_fencing_then_verifying_not_idle(self):
        adapter = self.adapter()
        group = self.take(device_id="a")
        forward = {"direction": "reverse", "local": "tcp:8765", "remote": "tcp:8766"}
        self.record(group, self.work(forwards=[forward]))
        self.broker.recovery_io, self.broker.recovery_owner, self.broker.ops_issue = (
            adapter,
            "ops",
            "CHE-fixture",
        )
        live = {1234: IDENTITY}
        calls = []

        def stop(identity, timeout):
            calls.append(("stop", identity))
            live.pop(identity["pid"], None)
            return True

        def adb(group, device, args, timeout):
            calls.append(tuple(args))
            if args[-1:] == ["--list"]:
                return (
                    "usb-a tcp:8765 tcp:8766\n"
                    if args[0] == "reverse" and not any("--remove" in c for c in calls)
                    else ""
                )
            return ""

        with (
            patch.object(adapter, "_identity", side_effect=lambda pid: live.get(pid)),
            patch.object(adapter, "_descendants", return_value=set()),
            patch.object(adapter, "_daemon_pid", return_value=999),
            patch.object(adapter, "_stop", side_effect=stop),
            patch.object(adapter, "_adb", side_effect=adb),
        ):
            self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")
            self.broker.maintain()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM broker_groups").fetchone()[0], "verifying"
            )
        self.assertIn(("reverse", "--remove", "tcp:8765"), calls)
        self.assertEqual(sum(c[0] == "stop" for c in calls), 1)

    def test_real_root_stop_reaches_verifying_without_mocking_process_checks(self):
        worker, identity = self.worker()
        group = self.take(device_id="a", binding=identity)
        self.record(group, self.work(processes=[identity]))
        adapter = self.adapter()
        self.broker.recovery_io, self.broker.recovery_owner, self.broker.ops_issue = (
            adapter,
            "ops",
            "CHE-fixture",
        )
        with (
            patch.object(adapter, "_daemon_pid", return_value=os.getpid()),
            patch.object(adapter, "_adb", return_value=""),
        ):
            self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")
            self.broker.maintain()
        worker.wait(timeout=5)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM broker_groups").fetchone()[0], "verifying"
            )

    def test_manifest_without_binding_root_refuses_and_leaves_root_alive(self):
        worker, identity = self.worker()
        group = self.take(device_id="a", binding=identity)
        self.record(group, self.work(processes=[]))
        adapter = self.adapter()
        with (
            patch.object(adapter, "_daemon_pid", return_value=os.getpid()),
            patch.object(adapter, "_adb", return_value=""),
        ):
            self.assertFalse(adapter.fence(group, timeout=2))
        self.assertIsNone(worker.poll())

    def test_setting_compare_restore_verify_and_failed_undo_quarantine(self):
        adapter = self.adapter()
        group = self.group()
        change = {
            "kind": "setting",
            "device_id": "a",
            "name": "brightness",
            "before": "100",
            "after": "20",
        }
        self.record(group, change)
        for initial, success in [("20", True), ("100", True), ("other", False)]:
            value = [initial]

            def adb(group, device, args, timeout):
                if "put" in args:
                    value[0] = args[-1]
                return value[0] + "\n"

            with patch.object(adapter, "_adb", side_effect=adb):
                self.assertEqual(adapter.undo(group, change, timeout=2), success)
                if success:
                    self.assertEqual(value[0], "100")
        self.broker.recovery_io, self.broker.recovery_owner, self.broker.ops_issue = (
            adapter,
            "ops",
            "CHE-fixture",
        )
        with (
            patch.object(adapter, "fence", return_value=True),
            patch.object(adapter, "_adb", side_effect=TimeoutError),
        ):
            self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")
            self.broker.maintain()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM broker_groups").fetchone()[0], "quarantine"
            )

    def test_forward_alias_is_part_of_the_same_device_census(self):
        adapter = self.adapter()
        adapter.registry = self.registry
        group = self.group()
        with patch.object(adapter, "_adb", return_value="192.0.2.1:5555 tcp:8765 tcp:8766\n"):
            self.assertEqual(adapter._forwards(group, "a", "forward", 2), {"tcp:8765": "tcp:8766"})
        with patch.object(adapter, "_adb", return_value="unknown tcp:8765 tcp:8766\n"):
            with self.assertRaises(ValueError):
                adapter._forwards(group, "a", "forward", 2)

    def test_app_absence_needs_valid_package_readback(self):
        adapter = self.adapter()
        group = self.group()
        change = {
            "kind": "app",
            "device_id": "a",
            "package": "org.example.fixture",
            "installed_by_run": True,
            "allowed_remove": True,
        }
        self.record(group, change)
        with patch.object(adapter, "_adb", return_value="Error: package manager unavailable"):
            self.assertFalse(adapter.undo(group, change, timeout=2))
        with patch.object(
            adapter, "_adb", side_effect=["package:org.example.fixture", "Success", ""]
        ):
            self.assertTrue(adapter.undo(group, change, timeout=2))

    def test_missing_root_cannot_prove_no_orphaned_work(self):
        adapter = self.adapter()
        with patch.object(adapter, "_identity", return_value=None):
            with self.assertRaises(ValueError):
                adapter._descendants(IDENTITY)
            self.assertEqual(adapter._descendants(IDENTITY, allow_missing=True), set())
        with patch.object(adapter, "_identity", return_value=dict(IDENTITY, process_start="other")):
            with self.assertRaises(ValueError):
                adapter._descendants(IDENTITY, allow_missing=True)

    def test_deadline_unknown_forward_and_unrecorded_undo_fail_closed(self):
        adapter = self.adapter()
        group = self.group()
        with patch.object(adapter, "_stop") as stop:
            self.assertFalse(adapter.fence(group, timeout=0))
            stop.assert_not_called()
        unrecorded = {"kind": "home", "device_id": "a", "allowed": True}
        self.assertFalse(adapter.undo(group, unrecorded, timeout=2))
        with (
            patch.object(adapter, "_identity", return_value=None),
            patch.object(adapter, "_descendants", return_value=set()),
            patch.object(adapter, "_daemon_pid", return_value=999),
            patch.object(adapter, "_adb", return_value="usb-a tcp:9999 tcp:9998\n"),
        ):
            self.assertFalse(adapter.fence(group, timeout=2))


if __name__ == "__main__":
    unittest.main()
