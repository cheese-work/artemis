"""DLP-1 recovery contract; fake gateway, device actions and Multica claims only."""

import json
from pathlib import Path
import sqlite3
import unittest

import test_device_lease_broker as fixtures


class FakeRecovery:
    def __init__(self):
        self.stream_open = True
        self.credential_live = True
        self.work_running = True
        self.cleanup_ok = True
        self.calls = []

    def fence(self, group, *, timeout):
        self.calls.append(("fence", group["run_id"]))
        self.credential_live = self.stream_open = self.work_running = False
        return True

    def undo(self, group, change, *, timeout):
        self.calls.append(("undo", change))
        return self.cleanup_ok


class RecoveryTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take
    refused = fixtures.BrokerTests.refused

    def configure(self):
        self.io = FakeRecovery()
        self.broker.recovery_io = self.io
        self.broker.recovery_owner = "ops-fixture"
        self.broker.ops_issue = "CHE-recovery"

    def state(self, group):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(
                "SELECT state FROM broker_groups WHERE lease_id=?", (group["lease_id"],)
            ).fetchone()[0]

    def release(self, group):
        return self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")

    def test_failed_cleanup_quarantines_and_never_regrants(self):
        self.configure()
        group = self.take(device_id="a")
        self.io.cleanup_ok = False
        # An unconfirmed fence is failed recovery, even before undo starts.
        self.io.fence = lambda group, **kw: False
        self.release(group)
        self.broker.maintain()
        self.assertEqual(self.state(group), "quarantine")
        busy = self.refused(
            "device_busy",
            lambda: self.take(
                run_id="next", purpose="CHE-next", idempotency_key="next", device_id="a"
            ),
        )
        self.assertEqual(busy.details["holder"]["recovery_owner"], "ops-fixture")
        self.assertEqual(busy.details["holder"]["ops_issue"], "CHE-recovery")

    def test_failed_recorded_undo_quarantines_and_never_regrants(self):
        self.configure()
        group = self.take(device_id="a")
        self.io.cleanup_ok = False
        self.broker.record_change(
            lease_id=group["lease_id"],
            principal="agent",
            run_id="run-1",
            change={
                "kind": "setting",
                "device_id": "a",
                "name": "brightness",
                "before": "100",
                "after": "20",
            },
        )
        self.release(group)
        self.broker.maintain()
        self.assertEqual(self.state(group), "quarantine")
        busy = self.refused(
            "device_busy",
            lambda: self.take(
                run_id="next", purpose="CHE-next", idempotency_key="next", device_id="a"
            ),
        )
        self.assertEqual(busy.details["holder"]["recovery_owner"], "ops-fixture")
        self.assertEqual(busy.details["holder"]["ops_issue"], "CHE-recovery")

    def test_open_stream_at_expiry_closes_before_verification_and_regrant(self):
        self.configure()
        group = self.take(device_id="a")
        self.now = group["expires_at"]
        self.broker.maintain()
        self.assertFalse(self.io.stream_open, "expiry must close existing streams")
        self.assertFalse(self.io.credential_live)
        self.assertFalse(self.io.work_running)
        self.assertEqual(self.state(group), "verifying")
        self.refused(
            "device_busy",
            lambda: self.take(
                run_id="next", purpose="CHE-next", idempotency_key="next", device_id="a"
            ),
        )

    def blocked_source(self, source, alias):
        self.broker.admission_sources = {"artemis": lambda: [], "android": lambda: []}
        self.broker.admission_sources[source] = lambda: [alias]
        with self.assertRaises(fixtures.Refused, msg="existing claims must win"):
            self.take(device_id="a")
        self.assertEqual(self.take(device_id="b", purpose="CHE-disjoint")["state"], "active")

    def test_execution_lock_blocks_wireless_alias_admission(self):
        self.blocked_source("artemis", "192.0.2.1:5555")

    def test_android_claim_blocks_usb_alias_admission(self):
        self.blocked_source("android", "usb-a")

    def test_missing_claim_source_closes_admission(self):
        self.broker.admission_sources = {"artemis": lambda: []}
        self.refused("admission_unavailable", lambda: self.take(device_id="a"))

    def test_missing_shared_claim_guard_closes_admission(self):
        from contextlib import nullcontext

        self.broker.admission_guard = nullcontext
        self.refused("admission_unavailable", lambda: self.take(device_id="a"))

    def test_audit_is_append_only_jsonl_with_ict_and_actor_device_context(self):
        group = self.take(device_id="a", agent_id="agent-label", issue_id="CHE-audit")
        self.release(group)
        audit = Path(str(self.path) + ".audit.jsonl")
        self.assertTrue(audit.exists(), "committed events must reach the JSONL audit")
        before = audit.read_bytes()
        self.broker.maintain()
        self.assertTrue(audit.read_bytes().startswith(before))
        rows = [json.loads(line) for line in audit.read_text().splitlines()]
        self.assertEqual([r["event"] for r in rows], ["acquire", "release", "fence"])
        for row in rows:
            self.assertTrue(row["time"].endswith(" ICT"))
            self.assertEqual(row["agent"], "agent-label")
            self.assertEqual(row["run_id"], "run-1")
            self.assertEqual(row["issue"], "CHE-audit")
            self.assertEqual(row["device_id"], "a")

    def test_admission_failure_does_not_prevent_expiry_fencing(self):
        self.configure()
        group = self.take(device_id="a")

        def unavailable():
            raise OSError("fake Multica unavailable")

        self.broker.admission_sources = {"android": unavailable}
        self.now = group["expires_at"]
        self.broker.maintain()
        self.assertFalse(self.io.stream_open)
        self.assertEqual(self.state(group), "verifying")
        self.refused(
            "admission_unavailable",
            lambda: self.take(
                device_id="b", run_id="new", purpose="CHE-new", idempotency_key="new"
            ),
        )

    def test_overdue_recovery_still_attempts_fencing_before_quarantine(self):
        self.configure()
        group = self.take(device_id="a")
        self.now = group["expires_at"] + 121
        self.broker.maintain()
        self.assertFalse(self.io.stream_open, "a late recovery must still revoke access")
        self.assertEqual(self.state(group), "quarantine")

    def test_damaged_audit_closes_admission_but_still_fences_expired_access(self):
        self.configure()
        group = self.take(device_id="a")
        Path(str(self.path) + ".audit.jsonl").write_text("corrupted prefix\n")
        self.now = group["expires_at"]
        self.broker.maintain()
        self.assertFalse(self.io.stream_open)
        self.assertNotEqual(self.state(group), "released")
        with self.assertRaises((RuntimeError, OSError, fixtures.Refused)):
            self.take(run_id="next", purpose="CHE-next", idempotency_key="next", device_id="b")

    def test_non_utf8_audit_does_not_prevent_expiry_fencing(self):
        self.configure()
        group = self.take(device_id="a")
        Path(str(self.path) + ".audit.jsonl").write_bytes(b"\xff\n")
        self.now = group["expires_at"]
        self.broker.maintain()
        self.assertFalse(self.io.stream_open)
        self.assertEqual(self.state(group), "verifying")

    def test_cleanup_and_verification_run_without_sqlite_writer_lock(self):
        self.configure()
        group = self.take(device_id="a")
        changes = [
            {
                "kind": "setting",
                "device_id": "a",
                "name": "brightness",
                "before": "100",
                "after": "20",
            },
            {
                "kind": "app",
                "device_id": "a",
                "package": "fixture.app",
                "installed_by_run": True,
                "allowed_remove": True,
            },
        ]
        for change in changes:
            self.broker.record_change(
                lease_id=group["lease_id"], principal="agent", run_id="run-1", change=change
            )
        original = self.io.undo

        def undo(group, change, **kwargs):
            with sqlite3.connect(self.path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
            return original(group, change, **kwargs)

        self.io.undo = undo
        self.release(group)
        self.broker.maintain()
        self.assertEqual(self.state(group), "verifying")
        self.assertEqual([c[1] for c in self.io.calls if c[0] == "undo"], list(reversed(changes)))
        self.refused(
            "leader_required",
            lambda: self.broker.verify_release(
                group["lease_id"], verifier="holder", evidence="fake-evidence", passed=True
            ),
        )
        self.broker.verify_release(
            group["lease_id"],
            verifier="5cd75ce5-d323-4004-9779-7e2ccf16bde4",
            evidence="fixture-evidence",
            passed=True,
        )
        self.assertEqual(
            self.take(device_id="a", run_id="next", purpose="CHE-next", idempotency_key="next")[
                "state"
            ],
            "active",
        )

    def test_forbidden_cleanup_intents_are_refused_without_device_io(self):
        self.configure()
        group = self.take(device_id="a")
        for change in (
            {
                "kind": "app",
                "device_id": "a",
                "package": "preexisting",
                "installed_by_run": False,
                "allowed_remove": True,
            },
            {"kind": "home", "device_id": "a", "allowed": False},
            {"kind": "shell", "device_id": "a", "command": "wipe"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(fixtures.Refused):
                    self.broker.record_change(
                        lease_id=group["lease_id"], principal="agent", run_id="run-1", change=change
                    )
        self.release(group)
        self.broker.maintain()
        self.assertEqual(self.io.calls, [("fence", "run-1")])

    def test_audit_retries_after_export_failure_without_duplicate_or_lost_commit(self):
        import audit
        from unittest.mock import patch

        real = audit.flush
        calls = 0

        def fail_second(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("fake disk failure after commit")
            return real(path)

        with patch.object(audit, "flush", fail_second):
            with self.assertRaises(OSError):
                self.take(device_id="a")
        group = self.take(device_id="a")
        self.assertEqual(group["state"], "active")
        rows = [
            json.loads(line)
            for line in Path(str(self.path) + ".audit.jsonl").read_text().splitlines()
        ]
        self.assertEqual([r["event"] for r in rows], ["acquire"])

    def test_failed_verification_needs_named_owner_and_manual_recovery(self):
        self.configure()
        group = self.take(device_id="a")
        self.release(group)
        self.broker.maintain()
        self.broker.verify_release(
            group["lease_id"],
            verifier="5cd75ce5-d323-4004-9779-7e2ccf16bde4",
            evidence="fixture-failure",
            passed=False,
        )
        self.assertEqual(self.state(group), "quarantine")
        self.refused(
            "recovery_owner_required",
            lambda: self.broker.recover_quarantine(
                group["lease_id"], recovery_owner="other", evidence="fixed"
            ),
        )
        self.broker.recover_quarantine(
            group["lease_id"], recovery_owner="ops-fixture", evidence="fixed"
        )
        self.assertEqual(self.state(group), "verifying")


if __name__ == "__main__":
    unittest.main()
