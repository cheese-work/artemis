"""Rig pool allocation: synthetic devices only, never live phone access."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import unittest

import test_device_lease_broker as fixtures
from broker import AdmissionSnapshot, Device, DeviceRegistry


class PoolTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take
    refused = fixtures.BrokerTests.refused

    def allocate(self, run, **changes):
        return self.take(run_id=run, purpose=f"CHE-{run}", idempotency_key=run, **changes)

    def free(self, group):
        self.broker.release(lease_id=group["lease_id"], principal="agent", run_id=group["run_id"])
        self.broker.recovery(group["lease_id"], "fencing", "cleaning")
        self.broker.recovery(group["lease_id"], "cleaning", "verifying")
        self.broker.verify_release(
            group["lease_id"],
            verifier=self.broker.leader_id,
            evidence="fixture independent cleanup",
            passed=True,
        )

    def test_two_concurrent_runs_hold_different_phones_and_third_waits(self):
        start = threading.Barrier(2)

        def acquire(run):
            start.wait()
            return self.allocate(run)

        with ThreadPoolExecutor(2) as pool:
            groups = list(pool.map(acquire, ["one", "two"]))
        self.assertEqual({g["devices"][0]["device_id"] for g in groups}, {"a", "b"})
        self.assertEqual(len({g["lease_id"] for g in groups}), 2)
        busy = self.refused("device_busy", lambda: self.allocate("three"))
        self.assertEqual(busy.details["ticket"]["state"], "queued")
        for group in groups:
            self.assertEqual(
                self.broker.status(
                    lease_id=group["lease_id"], principal="agent", run_id=group["run_id"]
                )["state"],
                "active",
            )

    def test_idle_order_survives_reopen_and_uses_release_time(self):
        first = self.allocate("one", device_id="a")
        self.now += 10
        second = self.allocate("two", device_id="b")
        self.now += 10
        self.free(second)
        self.now += 10
        self.free(first)  # b has been idle longer, despite a's older acquire.
        self.broker = self.new_broker()
        self.assertEqual(self.allocate("three")["devices"][0]["device_id"], "b")

    def test_unused_phone_precedes_previously_used_and_exact_alias_overrides_order(self):
        self.free(self.allocate("one", device_id="a"))
        self.assertEqual(self.allocate("two")["devices"][0]["device_id"], "b")
        exact = self.allocate("three", device_id="usb-a")
        self.assertEqual(exact["devices"][0]["device_id"], "a")
        self.refused(
            "run_already_holds",
            lambda: self.take(
                run_id="three", purpose="CHE-three", idempotency_key="nested", device_id="a"
            ),
        )

    def test_quarantined_phone_is_never_selected(self):
        group = self.allocate("one", device_id="a")
        self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="one")
        self.broker.recovery(
            group["lease_id"], "fencing", "quarantine", recovery_owner="ops", ops_issue="CHE-ops"
        )
        self.assertEqual(self.allocate("two")["devices"][0]["device_id"], "b")
        self.refused("device_busy", lambda: self.allocate("three"))

    def configure_classes(self):
        self.registry = DeviceRegistry(
            [
                Device(
                    "a",
                    ("usb-a",),
                    model="Galaxy A6",
                    api=29,
                    restricted=False,
                    identity_confirmed=True,
                ),
                Device(
                    "b",
                    ("usb-b",),
                    model="Galaxy S9",
                    api=28,
                    restricted=False,
                    identity_confirmed=True,
                ),
            ]
        )
        self.broker = self.new_broker()

    def test_model_and_api_filters_and_exact_mismatch(self):
        self.configure_classes()
        self.assertEqual(
            self.allocate("one", model="Galaxy S9", api=28)["devices"][0]["device_id"], "b"
        )
        self.refused("impossible_batch", lambda: self.allocate("two", device_id="a", api=28))
        self.refused("impossible_batch", lambda: self.allocate("two", model="unknown"))
        self.assertEqual(self.allocate("two", api=29)["devices"][0]["device_id"], "a")

    def test_waiting_filter_persists_and_never_offers_wrong_class(self):
        self.configure_classes()
        holder = self.allocate("one", device_id="b")
        ticket = self.refused("device_busy", lambda: self.allocate("two", api=28)).details["ticket"]
        self.broker.park(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-two",
            run_id="two",
        )
        self.broker = self.new_broker()
        args = dict(
            ticket_id=ticket["ticket_id"], principal="agent", agent_id="agent", issue_id="CHE-two"
        )
        self.assertEqual(self.broker.ticket_status(**args)["state"], "queued")
        self.free(holder)
        offered = self.broker.ticket_status(**args)
        self.assertEqual(offered["reserved"], ["b"])
        self.assertEqual(offered["api"], 28)

    def test_invalid_filter_and_idempotency_conflict(self):
        for changes in ({"api": True}, {"api": 0}, {"model": " "}, {"model": 123}):
            with self.subTest(changes=changes):
                self.refused("invalid_request", lambda: self.allocate("one", **changes))
        self.configure_classes()
        self.allocate("one", api=29)
        self.refused("idempotency_conflict", lambda: self.allocate("one", api=28))

    def test_idle_history_without_new_usage_table_uses_historical_leases(self):
        group = self.allocate("one", device_id="a")
        self.free(group)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE broker_device_usage")
        self.broker = self.new_broker()
        self.assertEqual(self.allocate("two")["devices"][0]["device_id"], "b")

    def readiness(self, expiry):
        self.broker.admission_snapshot = lambda: AdmissionSnapshot(
            {"a": "usb-a", "b": "usb-b"},
            {
                device: dict(reference="CHE-9610", observed_at=self.now, expires_at=expiry)
                for device in ("a", "b")
            },
        )

    def test_claim_and_renew_never_outlive_their_original_authority(self):
        self.readiness(self.now + 400)
        group = self.allocate("one")
        self.now += 200
        self.readiness(self.now + 1000)  # New owner authority does not extend the old claim.
        renewed = self.broker.renew(
            lease_id=group["lease_id"],
            principal="agent",
            run_id="one",
            ttl=300,
            idempotency_key="renew",
        )
        self.assertEqual(renewed["expires_at"], 100_400)
        self.now = 100_400
        self.assertEqual(
            self.broker.status(lease_id=group["lease_id"], principal="agent", run_id="one")[
                "state"
            ],
            "fencing",
        )

    def test_acquire_clips_to_authority_and_expired_authority_grants_nothing(self):
        self.readiness(self.now + 100)
        self.assertEqual(self.allocate("one")["expires_at"], self.now + 100)
        self.readiness(self.now)
        self.refused("admission_unavailable", lambda: self.allocate("two"))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM broker_claim_grants").fetchone()[0], 1
            )

    def test_interrupted_batch_rolls_back_claim_grants_and_idle_history(self):
        self.readiness(self.now + 600)
        calls = []

        def interrupt(name):
            if name == "acquire:device":
                calls.append(name)
                if len(calls) == 2:
                    raise RuntimeError("fixture interrupted second device")

        self.broker.boundary = interrupt
        with self.assertRaisesRegex(RuntimeError, "second device"):
            self.allocate("one", count=2)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM broker_claim_grants").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM broker_device_usage").fetchone()[0], 0
            )
        self.broker.boundary = lambda name: None
        self.assertEqual(len(self.allocate("one", count=2)["grants"]), 2)

    def test_offline_projection_matches_filtered_pool_grants_and_idle_order(self):
        from shadow import smartqa_decide

        self.configure_classes()
        self.readiness(self.now + 600)
        self.free(self.allocate("one", api=29))
        self.allocate("two")  # b is unused; a is free but was already used.
        with sqlite3.connect(self.path) as conn:
            entries = [
                json.loads(row[0]) for row in conn.execute("SELECT entry FROM broker_decisions")
            ]
        acquires = [entry for entry in entries if entry["operation"] == "acquire"]
        self.assertEqual(len(acquires), 2)
        for entry in acquires:
            expected = dict(entry.pop("broker"))
            expected.pop("lease_id")
            self.assertEqual(smartqa_decide(json.dumps(entry)), expected)


if __name__ == "__main__":
    unittest.main()
