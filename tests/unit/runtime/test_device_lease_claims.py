"""Claims authority, aliases and two-process exclusion; no device I/O."""

import importlib.util
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_compose as fixtures
import compose
from broker import Broker, Device, DeviceRegistry, Refused


def sdk_lock(root, alias):
    # Import the real SDK writer without the SDK's unrelated optional dependencies.
    paths = types.ModuleType("artemis.config.paths")
    paths.get_temp_dir = lambda name: Path(root) / "locks"
    probe = types.ModuleType("artemis.runtime.process_probe")
    probe.pid_is_alive = lambda *args: True
    spec = importlib.util.spec_from_file_location(
        "claims_sdk_lock", fixtures.ROOT / "artemis/runtime/device_lock.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {spec.name: module, "artemis.config.paths": paths, "artemis.runtime.process_probe": probe},
    ):
        spec.loader.exec_module(module)
    return module.DeviceExecutionLock(alias)


def race_writer(root, reg, writer, event, output):
    from device_lease.claims import ClaimLedger

    ledger = ClaimLedger(
        Path(root) / "leases.sqlite3",
        compose.FileGuard(Path(root) / "claims.guard"),
        reg,
        Path(root) / "locks",
        max_age=60,
    )
    event.wait(5)
    try:
        if writer == "android":
            ledger.claim("usb-rig-1", owner="android", run_id="r", reference="CHE-fixture")
        elif writer == "sdk":
            with patch.dict(
                os.environ,
                {
                    "ARTEMIS_DEVICE_ADMISSION_GUARD": str(Path(root) / "claims.guard"),
                    "ARTEMIS_DEVICE_CLAIMS_DB": str(Path(root) / "leases.sqlite3"),
                },
            ):
                if not sdk_lock(root, "usb-rig-1")._try_acquire_owner_lock():
                    raise ValueError("busy")
        else:
            broker = Broker(
                Path(root) / "leases.sqlite3",
                reg,
                admitted={"rig-1": "usb-rig-1"},
                admission_guard=ledger.guard,
                admission_sources={
                    "android": ledger.read,
                    "artemis": compose.artemis_locks_reader(Path(root) / "locks"),
                },
            )
            broker.acquire(
                principal="broker",
                run_id="r",
                purpose="CHE-race",
                ttl=300,
                idempotency_key="race",
                device_id="rig-1",
            )
        output.put((writer, True))
    except (ValueError, Refused):
        output.put((writer, False))


class ClaimsTests(unittest.TestCase):
    setUp = fixtures.ComposeTests.setUp
    write_json = fixtures.ComposeTests.write_json
    write_admitted = fixtures.ComposeTests.write_admitted
    config_file = fixtures.ComposeTests.config_file
    composed_broker = fixtures.ComposeTests.composed_broker
    take = fixtures.ComposeTests.take

    def test_avd_reload_requires_new_generation_and_retains_claims_and_quarantine(self):
        from device_lease.claims import ClaimLedger

        self.live()
        avd = next(
            device
            for device in DeviceRegistry.load(
                fixtures.ROOT / "tests/fixtures/device_lease/avd.json"
            ).devices()
            if device.kind == "avd"
        )
        claim = self.ledger.claim("usb-rig-1", owner="android", run_id="run", reference="fixture")

        def candidate(device):
            return ClaimLedger(
                self.ledger.db_path,
                self.ledger.guard,
                DeviceRegistry([*self.reg.devices(), device]),
                self.ledger.lock_dir,
            )

        def census(ledger, generation=2, **updates):
            packet = dict(
                generation=generation,
                observed_at=time.time(),
                complete=True,
                writers=["android", "artemis"],
                claims=self.ledger.snapshot()["claims"],
            )
            ledger.census(**(packet | updates))

        original = candidate(avd)
        for changes in ({"observed_at": 0}, {"claims": []}, {"generation": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                census(original, **changes)
        census(original)
        self.assertEqual(original.aliases[avd.aliases[0]], avd.device_id)
        self.assertEqual(original.snapshot()["claims"][0]["claim_id"], claim)

        changed = candidate(replace(avd, config_sha256="d" * 64))
        with self.assertRaises(ValueError):
            census(changed)
        with sqlite3.connect(self.ledger.db_path) as conn:
            conn.execute(
                "INSERT INTO broker_groups VALUES ('fixture','owner','run','test',0,1,1,1,'quarantine',NULL,NULL)"
            )
        with self.assertRaises(ValueError):
            census(changed, generation=3)
        census(original, generation=3)
        with sqlite3.connect(self.ledger.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM broker_groups").fetchone(), ("quarantine",)
            )
            conn.execute("UPDATE broker_groups SET state='released'")
            conn.execute(
                "INSERT INTO broker_tickets (ticket_id, principal, agent_id, issue_id, pool, count, ttl, position, state) VALUES ('ticket','owner','agent','issue','rig',1,300,1,'offered')"
            )
        with self.assertRaises(ValueError):
            census(changed, generation=4)
        with sqlite3.connect(self.ledger.db_path) as conn:
            conn.execute("UPDATE broker_tickets SET state='cancelled'")
        census(changed, generation=4)

    def test_unverified_snapshot_writer_cannot_overlap_grant(self):
        broker, composed = self.composed_broker()
        self.take(broker, device_id="rig-1")
        with self.assertRaises(ValueError):
            compose.write_claims_snapshot(
                self.root / "android.json", composed["guard"], ["usb-rig-1"]
            )

    def live(self, census=True):
        from device_lease.claims import ClaimLedger

        self.reg = DeviceRegistry(
            [
                Device(
                    "rig-1",
                    ("usb-rig-1", "10.0.0.1:5555"),
                    restricted=False,
                    identity_confirmed=True,
                )
            ]
        )
        guard = compose.FileGuard(self.root / "claims.guard")
        self.ledger = ClaimLedger(
            self.root / "leases.sqlite3", guard, self.reg, self.root / "locks", max_age=60
        )
        self.broker = Broker(
            self.root / "leases.sqlite3",
            self.reg,
            admitted={"rig-1": "usb-rig-1"},
            admission_guard=guard,
            admission_sources={
                "android": self.ledger.read,
                "artemis": compose.artemis_locks_reader(self.root / "locks"),
            },
        )
        self.broker.inventory()  # Initialize broker schema, not claim authority.
        if census:
            self.ledger.census(
                generation=1,
                observed_at=time.time(),
                complete=True,
                writers=["android", "artemis"],
                claims=[],
            )
        return self.ledger

    def test_empty_uninitialised_incomplete_and_stale_census_close_admission(self):
        ledger = self.live(False)
        self.assertFalse(self.broker.inventory()["admission_available"])
        for complete, writers in [(False, ["android", "artemis"]), (True, ["android"])]:
            with self.assertRaises(ValueError):
                ledger.census(
                    generation=1,
                    observed_at=time.time(),
                    complete=complete,
                    writers=writers,
                    claims=[],
                )
            self.assertFalse(self.broker.inventory()["admission_available"])
        ledger.census(
            generation=1,
            observed_at=time.time(),
            complete=True,
            writers=["android", "artemis"],
            claims=[],
        )
        self.assertTrue(self.broker.inventory()["admission_available"])
        with sqlite3.connect(ledger.db_path) as conn:
            conn.execute("UPDATE android_claim_source SET observed_at=0")
        self.assertFalse(self.broker.inventory()["admission_available"])

    def test_claim_then_grant_and_grant_then_claim_with_wireless_alias(self):
        ledger = self.live()
        claim = ledger.claim("10.0.0.1:5555", owner="android", run_id="r", reference="CHE-fixture")
        with self.assertRaises(Refused) as caught:
            self.take(self.broker, device_id="usb-rig-1")
        self.cancel(caught.exception)
        with self.assertRaises(ValueError):
            ledger.release(claim, owner="other", run_id="r", actor="cli:fixture")
        ledger.release(claim, owner="android", run_id="r", actor="cli:fixture")
        self.take(self.broker, device_id="usb-rig-1", run_id="next", idempotency_key="next")
        with self.assertRaises(ValueError):
            ledger.claim("10.0.0.1:5555", owner="android", run_id="r", reference="CHE-fixture")

    def receipts(self):
        with sqlite3.connect(self.ledger.db_path) as conn:
            rows = conn.execute("SELECT payload FROM broker_audit ORDER BY seq").fetchall()
        return [json.loads(row[0]) for row in rows]

    def test_release_records_a_receipt_naming_the_actor_apart_from_the_owner(self):
        ledger = self.live()
        claim = ledger.claim("10.0.0.1:5555", owner="android", run_id="r", reference="CHE-fixture")
        before = self.receipts()
        ledger.release(
            claim, owner="android", run_id="r", actor="operator:x99", actor_run_id="op-1"
        )
        receipt = self.receipts()[len(before) :]
        self.assertEqual(len(receipt), 1)
        self.assertEqual(
            {k: receipt[0][k] for k in receipt[0] if k not in ("time",)},
            dict(
                event="claim_release",
                agent="android",
                run_id="r",
                issue="CHE-fixture",
                device_id="rig-1",
                claim_id=claim,
                actor="operator:x99",
                actor_run_id="op-1",
                actor_verified=False,
                outcome="deleted",
            ),
        )
        self.assertRegex(receipt[0]["time"], r" ICT$")
        self.assertEqual(self.receipts()[: len(before)], before)  # history is never rewritten

    def test_release_without_an_actor_is_refused_and_keeps_the_claim(self):
        ledger = self.live()
        claim = ledger.claim("rig-1", owner="android", run_id="r", reference="CHE-fixture")
        with self.assertRaises(TypeError):
            ledger.release(claim, owner="android", run_id="r")
        with self.assertRaises(ValueError):
            ledger.release(claim, owner="android", run_id="r", actor=" ")
        self.assertEqual([c["claim_id"] for c in ledger.snapshot()["claims"]], [claim])

    def test_wrong_owner_release_leaves_claim_and_writes_no_receipt(self):
        ledger = self.live()
        claim = ledger.claim("rig-1", owner="android", run_id="r", reference="CHE-fixture")
        before = self.receipts()
        for owner, run in (("other", "r"), ("android", "other")):
            with self.assertRaises(ValueError):
                ledger.release(claim, owner=owner, run_id=run, actor="operator:x99")
        self.assertEqual([c["claim_id"] for c in ledger.snapshot()["claims"]], [claim])
        self.assertEqual(self.receipts(), before)

    def test_receipt_failure_rolls_back_the_deletion(self):
        ledger = self.live()
        claim = ledger.claim("rig-1", owner="android", run_id="r", reference="CHE-fixture")
        before = self.receipts()
        with patch("audit.enqueue", side_effect=RuntimeError("fixture audit failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture audit failure"):
                ledger.release(claim, owner="android", run_id="r", actor="operator:x99")
        self.assertEqual([c["claim_id"] for c in ledger.snapshot()["claims"]], [claim])
        self.assertEqual(self.receipts(), before)

    def test_offered_reservations_are_occupied_for_both_writers(self):
        ledger = self.live()
        held = self.take(self.broker, device_id="rig-1")
        try:
            self.take(
                self.broker,
                run_id="next",
                purpose="CHE-next",
                idempotency_key="next",
                device_id="rig-1",
            )
        except Refused as exc:
            ticket = exc.details["ticket"]
        with sqlite3.connect(ledger.db_path) as conn:
            conn.execute("UPDATE broker_groups SET state='released'")
        self.broker.ticket_status(
            ticket_id=ticket["ticket_id"], principal="agent", agent_id="agent", issue_id="CHE-next"
        )
        with self.assertRaises(ValueError):
            ledger.claim("10.0.0.1:5555", owner="android", run_id="r", reference="CHE-fixture")
        with self.sdk_env():
            self.assertFalse(sdk_lock(self.root, "10.0.0.1:5555")._try_acquire_owner_lock())

    def cancel(self, error):
        ticket = error.details["ticket"]
        self.broker.cancel_ticket(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
        )

    def sdk_env(self):
        return patch.dict(
            os.environ,
            {
                "ARTEMIS_DEVICE_ADMISSION_GUARD": str(self.root / "claims.guard"),
                "ARTEMIS_DEVICE_CLAIMS_DB": str(self.root / "leases.sqlite3"),
            },
        )

    def test_sdk_both_orders_and_aliases(self):
        ledger = self.live()
        with self.sdk_env():
            lock = sdk_lock(self.root, "10.0.0.1:5555")
            self.assertTrue(lock._try_acquire_owner_lock())
            with self.assertRaises(Refused) as caught:
                self.take(self.broker, device_id="usb-rig-1")
            self.cancel(caught.exception)
            with self.assertRaises(ValueError):
                ledger.claim("usb-rig-1", owner="a", run_id="r", reference="CHE-fixture")
            self.assertFalse(sdk_lock(self.root, "usb-rig-1")._try_acquire_owner_lock())
            lock.path.unlink()
            self.take(self.broker, device_id="usb-rig-1", run_id="next", idempotency_key="next")
            self.assertFalse(sdk_lock(self.root, "10.0.0.1:5555")._try_acquire_owner_lock())

    def test_census_cannot_erase_claims_and_mismatched_guard_closes_sdk(self):
        ledger = self.live()
        claim = ledger.claim("rig-1", owner="android", run_id="r", reference="CHE-fixture")
        with self.assertRaises(ValueError):
            ledger.census(
                generation=2,
                observed_at=time.time(),
                complete=True,
                writers=["android", "artemis"],
                claims=[],
            )
        self.assertEqual(ledger.read(), ["rig-1"])
        with (
            self.sdk_env(),
            patch.dict(
                os.environ, {"ARTEMIS_DEVICE_ADMISSION_GUARD": str(self.root / "wrong.guard")}
            ),
        ):
            self.assertFalse(sdk_lock(self.root, "usb-rig-1")._try_acquire_owner_lock())
        ledger.release(claim, owner="android", run_id="r", actor="cli:fixture")
        with self.sdk_env(), patch.dict(os.environ, {"ARTEMIS_DEVICE_CLAIMS_DB": ""}):
            self.assertFalse(sdk_lock(self.root, "usb-rig-1")._try_acquire_owner_lock())

    def test_separate_process_races_have_one_owner(self):
        self.live()
        ctx = multiprocessing.get_context("fork")
        for writer in ["android", "sdk"]:
            with self.subTest(writer=writer):
                event, output = ctx.Event(), ctx.Queue()
                workers = [
                    ctx.Process(target=race_writer, args=(self.root, self.reg, who, event, output))
                    for who in [writer, "broker"]
                ]
                for p in workers:
                    p.start()
                event.set()
                for p in workers:
                    p.join(10)
                    self.assertEqual(p.exitcode, 0)
                results = [output.get(timeout=2) for _ in workers]
                self.assertEqual(sum(success for _, success in results), 1, results)
                with sqlite3.connect(self.ledger.db_path) as conn:
                    conn.execute("DELETE FROM android_claims")
                    conn.execute("UPDATE broker_groups SET state='released'")
                    conn.execute("UPDATE broker_tickets SET state='cancelled'")
                for path in (self.root / "locks").glob("*.lock"):
                    path.unlink()
                output.close()
                output.join_thread()


if __name__ == "__main__":
    unittest.main()
