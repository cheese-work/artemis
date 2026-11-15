"""Real broker domain, synthetic identity, device policy and recovery I/O."""

from pathlib import Path
import asyncio
import copy
import sys
import unittest
from unittest.mock import patch
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit/runtime"))
import test_device_lease_broker as fixtures
from test_device_lease_recovery import FakeRecovery
import test_remote_attestation as attestation_fixtures
from remote_broker import BrokerAdmission, RemoteRecovery
from test_remote_adb import SSH, Listener, key
from remote_adb import RemoteAccess


class BrokerRemoteTests(unittest.TestCase):
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take

    def setUp(self):
        real = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real(*args, **kwargs)
            conn.execute("PRAGMA synchronous=OFF")
            return conn

        self.patcher = patch("sqlite3.connect", side_effect=connect)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        fixtures.BrokerTests.setUp(self)
        template = attestation_fixtures.AttestationTests()
        template.setUp()
        self.policy = dict(
            state="device",
            granted=True,
            serial="usb-a",
            revision="mapping-1",
            profile=template.profile,
            baseline=template.baseline,
            isolation="ops-reviewed-isolation-digest",
        )
        self.identity = dict(
            principal="agent", agent="agent", issue="CHE-fixture", run="run-1", lease=""
        )
        self.group = self.take(device_id="a", binding={"mode": "remote"})
        self.identity["lease"] = self.group["lease_id"]
        self.resolver = BrokerAdmission(
            self.broker,
            authenticate=lambda identity: True,
            generation=lambda: 1,
            inspect=lambda device: self.policy,
        )

    def test_remote_snapshot_joins_real_broker_identity_registry_and_attestation(self):
        current = self.resolver(self.identity)
        self.assertIsNotNone(current, "active authenticated remote lease must admit")
        self.assertEqual(current["devices"], (("a", "usb-a", "mapping-1"),))
        self.assertEqual(current["expires"], self.group["expires_at"])
        self.assertEqual(current["hard_deadline"], self.group["hard_deadline"])
        self.broker.release(lease_id=self.group["lease_id"], principal="agent", run_id="run-1")
        self.assertIsNone(self.resolver(self.identity))

    def test_authentication_and_each_device_evidence_loss_deny_current_access(self):
        self.assertIsNotNone(self.resolver(self.identity))
        for field in ("agent", "issue", "run", "principal"):
            self.assertIsNone(self.resolver(dict(self.identity, **{field: "wrong"})), field)
        for field in ("profile", "baseline", "isolation", "granted", "revision"):
            before = self.policy[field]
            self.policy[field] = None
            self.assertIsNone(self.resolver(self.identity), field)
            self.policy[field] = before

    def test_failed_independent_post_cleanup_attestation_quarantines(self):
        recovery = FakeRecovery()
        recovery.verify_state = lambda group, **kw: False
        self.broker.recovery_io = recovery
        self.broker.recovery_owner, self.broker.ops_issue = "owner", "CHE-recovery"
        self.broker.release(lease_id=self.group["lease_id"], principal="agent", run_id="run-1")
        self.broker.maintain()
        group = self.broker.status(
            lease_id=self.group["lease_id"], principal="agent", run_id="run-1"
        )
        self.assertEqual(group["state"], "quarantine")

    def test_remote_recovery_requires_independent_attestation_adapter(self):
        self.broker.recovery_io = FakeRecovery()  # deliberately has no verify_state
        self.broker.recovery_owner, self.broker.ops_issue = "owner", "CHE-recovery"
        self.broker.release(lease_id=self.group["lease_id"], principal="agent", run_id="run-1")
        self.broker.maintain()
        group = self.broker.status(
            lease_id=self.group["lease_id"], principal="agent", run_id="run-1"
        )
        self.assertEqual(group["state"], "quarantine", "remote attestation cannot be skipped")

    def test_release_evidence_must_match_durable_admission_not_a_new_baseline(self):
        async def listen(*args):
            return Listener()

        async def admit():
            access = RemoteAccess(
                Path(self.temp.name) / "keys.db",
                resolve=self.resolver,
                ops=SSH(),
                quarantine=lambda lease: None,
                clock=lambda: self.now,
                listen=listen,
                bootstrap=True,
            )
            self.assertIsNotNone(await access.issue(self.identity, key(1)))
            await access.close()  # release verification must use retained history after fencing
            return access

        access = asyncio.run(admit())
        clean = dict(
            profile=self.policy["profile"],
            baseline=self.policy["baseline"],
            observed=copy.deepcopy(self.policy["baseline"]),
        )
        recovery = RemoteRecovery(access, None, FakeRecovery(), inspection=lambda *a, **kw: clean)
        self.assertTrue(recovery.verify_state(self.group, timeout=1), "pinned clean state passes")
        for change in ("baseline", "profile"):
            evidence = copy.deepcopy(clean)
            if change == "baseline":
                user = next(iter(evidence["baseline"]["users"]))
                evidence["baseline"]["users"][user]["packages"] = "planted-post-lease-state"
                evidence["observed"] = copy.deepcopy(evidence["baseline"])
            else:
                evidence["profile"]["review"] = "different-release-time-profile"
            recovery.inspection = lambda *a, **kw: evidence
            with self.subTest(change=change):
                self.assertFalse(recovery.verify_state(self.group, timeout=1))
        recovery.inspection = lambda *a, **kw: clean
        for field in ("lease_id", "run_id", "holder"):
            with self.subTest(wrong_group=field):
                self.assertFalse(
                    recovery.verify_state(dict(self.group, **{field: "other"}), timeout=1)
                )
        with access.db() as conn:
            conn.execute("DELETE FROM r1_keys")
        self.assertFalse(
            recovery.verify_state(self.group, timeout=1), "missing history denies reuse"
        )


if __name__ == "__main__":
    unittest.main()
