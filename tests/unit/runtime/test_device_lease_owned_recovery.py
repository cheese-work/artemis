"""Real recovery policy with process/device I/O replaced at the boundary."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_broker as fixtures

IDENTITY = {"mode": "local", "pid": 1234, "process_start": "42", "boot_id": "fixture-boot"}


class OwnedRecoveryTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take

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
