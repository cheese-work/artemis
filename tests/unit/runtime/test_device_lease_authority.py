"""Authority storage and generation contract, synthetic devices only."""

import sqlite3
import unittest

import test_device_lease_resume as fixtures
from broker import Refused


class AuthorityTests(unittest.TestCase):
    setUp = fixtures.ResumeTests.setUp
    new_broker = fixtures.ResumeTests.new_broker
    take = fixtures.ResumeTests.take
    refused = fixtures.ResumeTests.refused
    busy = fixtures.ResumeTests.busy
    wait = fixtures.ResumeTests.wait
    free = fixtures.ResumeTests.free
    ticket = fixtures.ResumeTests.ticket
    owner = fixtures.ResumeTests.owner
    park = fixtures.ResumeTests.park
    claim = fixtures.ResumeTests.claim
    offered = fixtures.ResumeTests.offered

    def seed_authority(self, generation=1, state="enabled", name="broker"):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS broker_authority ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), name TEXT NOT NULL, "
                "generation INTEGER NOT NULL, state TEXT NOT NULL, fenced INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO broker_authority VALUES (1,?,?,?,0)",
                (name, generation, state),
            )

    def test_generation_bump_refuses_old_grant_including_replay(self):
        self.take(device_id="a")
        self.seed_authority(2, "fenced", "smartqa")
        self.refused("authority_stale", lambda: self.take(device_id="a"))
        self.refused(
            "authority_stale",
            lambda: self.take(
                run_id="new", purpose="CHE-new", idempotency_key="new", device_id="b"
            ),
        )

    def test_generation_bump_refuses_old_renew(self):
        group = self.take(device_id="a")
        self.now += 200
        self.seed_authority(2, "fenced", "smartqa")
        self.refused(
            "authority_stale",
            lambda: self.broker.renew(
                lease_id=group["lease_id"],
                principal="agent",
                run_id="run-1",
                ttl=300,
                idempotency_key="renew",
            ),
        )

    def test_generation_bump_refuses_old_claim(self):
        ticket = self.offered()
        self.seed_authority(2, "fenced", "smartqa")
        self.refused("authority_stale", lambda: self.claim(ticket))

    def test_persisted_drain_refuses_grants_renew_and_claim_but_allows_release(self):
        ticket = self.offered()
        group = self.take(device_id="b", purpose="CHE-second", idempotency_key="second")
        self.seed_authority(state="draining")
        self.refused("draining", lambda: self.claim(ticket))
        self.refused(
            "draining", lambda: self.take(run_id="new", purpose="CHE-new", idempotency_key="new")
        )
        self.now += 200
        self.refused(
            "draining",
            lambda: self.broker.renew(
                lease_id=group["lease_id"],
                principal="agent",
                run_id="run-1",
                ttl=300,
                idempotency_key="renew",
            ),
        )
        released = self.broker.release(
            lease_id=group["lease_id"], principal="agent", run_id="run-1"
        )
        self.assertEqual(released["state"], "fencing")

    def test_gateway_rejects_services_when_generation_access_check_fails(self):
        import importlib
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch

        # Load the stdlib parser without importing the optional LLM SDK facade.
        root = Path(__file__).resolve().parents[3]
        with patch.dict(sys.modules):
            for name, path in [
                ("artemis", root / "artemis"),
                ("artemis.runtime", root / "artemis/runtime"),
            ]:
                module = types.ModuleType(name)
                module.__path__ = [str(path)]
                sys.modules[name] = module
            Gateway = importlib.import_module("artemis.runtime.adb_gateway").Gateway

        gateway = Gateway(lambda: {"usb-a"})
        gateway.access = lambda: False
        self.assertFalse(gateway.allows("host:features"))
        self.assertFalse(gateway.allows("host:transport:usb-a"))

    def test_cutover_preserves_quarantine_and_requires_fence_and_release(self):
        from authority import Authority, GenerationGate

        control = Authority(self.broker, actor="ops-fixture", run_id="ops-run", issue="CHE-fixture")
        quarantined = self.take(device_id="a")
        self.broker.release(lease_id=quarantined["lease_id"], principal="agent", run_id="run-1")
        self.broker.recovery(
            quarantined["lease_id"],
            "fencing",
            "quarantine",
            recovery_owner="ops-owner",
            ops_issue="CHE-recovery",
        )
        active = self.take(
            device_id="b", run_id="other", purpose="CHE-other", idempotency_key="other"
        )
        self.refused("authority_not_fenced", lambda: control.enable("broker", 1))
        control.drain("broker", 1)
        self.assertEqual(control.readback()["state"], "draining")
        self.assertEqual(
            self.new_broker().status(
                lease_id=active["lease_id"], principal="agent", run_id="other"
            )["state"],
            "active",
        )
        self.refused("authority_not_fenced", lambda: control.enable("smartqa", 2))
        control.fence("broker", 1, "smartqa", gate=GenerationGate(control))
        self.refused("leases_unresolved", lambda: control.enable("smartqa", 2))
        self.broker.recovery(
            active["lease_id"],
            "fencing",
            "quarantine",
            recovery_owner="ops-other",
            ops_issue="CHE-other-recovery",
        )
        control.enable("smartqa", 2)
        readback = control.readback()
        self.assertEqual(
            (readback["name"], readback["generation"], readback["state"]), ("smartqa", 2, "enabled")
        )
        carried = next(
            g for g in readback["quarantine"] if g["lease_id"] == quarantined["lease_id"]
        )
        self.assertEqual(
            (carried["recovery_owner"], carried["ops_issue"]), ("ops-owner", "CHE-recovery")
        )
        new = self.new_broker(authority_name="smartqa", authority_generation=2)
        self.refused(
            "device_busy",
            lambda: self.take(
                new, device_id="a", run_id="next", purpose="CHE-next", idempotency_key="next"
            ),
        )

    def test_gateway_generation_change_closes_all_registered_streams(self):
        from authority import Authority, GenerationGate
        from unittest.mock import Mock

        control = Authority(self.broker, actor="ops-fixture", run_id="ops-run", issue="CHE-fixture")
        gate = GenerationGate(control)
        stream = Mock()
        gate.register("broker", 1, stream)
        self.assertTrue(gate.valid("broker", 1))
        control.drain("broker", 1)
        self.assertTrue(gate.valid("broker", 1), "drain lets existing access end")
        control.fence("broker", 1, "smartqa", gate=gate)
        stream.close.assert_called_once_with()
        self.assertFalse(gate.valid("broker", 1))
        self.refused("authority_stale", lambda: gate.register("broker", 1, Mock()))

    def test_failed_stream_close_holds_enable_and_retry_does_not_bump_twice(self):
        from authority import Authority, GenerationGate
        from unittest.mock import Mock

        control = Authority(self.broker, actor="ops-fixture", run_id="ops-run", issue="CHE-fixture")
        gate = GenerationGate(control)
        broken, other = Mock(), Mock()
        broken.close.side_effect = OSError("fake close failure")
        gate.register("broker", 1, broken)
        gate.register("broker", 1, other)
        control.drain("broker", 1)
        self.refused(
            "gateway_fence_failed", lambda: control.fence("broker", 1, "smartqa", gate=gate)
        )
        other.close.assert_called_once_with()
        self.refused("authority_not_fenced", lambda: control.enable("smartqa", 2))
        broken.close.side_effect = None
        control.fence("broker", 1, "smartqa", gate=gate)
        self.assertEqual(control.readback()["generation"], 2)
        control.enable("smartqa", 2)

    def test_fence_cannot_acknowledge_an_unrelated_gateway(self):
        from authority import Authority, GenerationGate
        from unittest.mock import Mock

        control = Authority(self.broker, actor="ops-fixture", run_id="ops-run", issue="CHE-fixture")
        unrelated = Mock()
        unrelated.readback.return_value = {
            "name": "different",
            "generation": 99,
            "state": "enabled",
        }
        gate = GenerationGate(unrelated)
        control.drain("broker", 1)
        self.refused("gateway_mismatch", lambda: control.fence("broker", 1, "smartqa", gate=gate))
        self.refused("authority_not_fenced", lambda: control.enable("smartqa", 2))

    def test_stale_successful_claim_and_renew_replays_are_also_refused(self):
        ticket = self.offered()
        group = self.claim(ticket)
        self.now += 200
        args = dict(
            lease_id=group["lease_id"],
            principal="agent",
            run_id="successor",
            ttl=300,
            idempotency_key="renew",
        )
        self.broker.renew(**args)
        self.seed_authority(2, "fenced", "smartqa")
        self.refused("authority_stale", lambda: self.claim(ticket))
        self.refused("authority_stale", lambda: self.broker.renew(**args))

    def test_track_closes_both_ends_if_generation_changes_during_registration(self):
        from authority import Authority, GenerationGate
        from unittest.mock import Mock

        control = Authority(self.broker, actor="ops-fixture", run_id="ops-run", issue="CHE-fixture")
        gate = GenerationGate(control)
        # Model commit between checking the two endpoints, before reconcile takes the lock.
        gate.valid = Mock(side_effect=[True, False])
        first, second = Mock(), Mock()
        with self.assertRaises(Refused):
            with gate.track("broker", 1, first, second):
                self.fail("stale stream entered the relay")
        self.assertGreaterEqual(first.close.call_count, 1)
        self.assertGreaterEqual(second.close.call_count, 1)
        self.assertEqual(gate.streams, {})


if __name__ == "__main__":
    unittest.main()
