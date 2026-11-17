"""Startup admission uses fake authority evidence and a local managed fixture, no ADB."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_managed_runs as managed_fixture
from broker import DeviceRegistry, Refused
import compose
from daemon import BrokerServer, ControlServer
from device_lease.claims import ClaimLedger


class StartupTests(unittest.TestCase):
    setUpFixtures = managed_fixture.ManagedRunTests.setUpFixtures
    new_broker = managed_fixture.ManagedRunTests.new_broker
    write_record = managed_fixture.ManagedRunTests.write_record
    refused = managed_fixture.ManagedRunTests.refused

    def setUp(self):
        managed_fixture.ManagedRunTests.setUp(self)
        self.registry = DeviceRegistry.load(
            Path(__file__).resolve().parents[2] / "fixtures/device_lease/avd.json"
        )
        self.device = next(d for d in self.registry.devices() if d.kind == "avd")
        self.broker.registry = self.registry
        self.broker.admitted = {}
        self.proof = {
            "generation": 1,
            "device": asdict(self.device),
            "principal": "agent",
            "agent_id": "agent",
            "issue_id": "CHE-fixture",
            "run_id": "run-1",
            "grant": {
                "reference": "startup-grant",
                "observed_at": self.now,
                "expires_at": self.now + 900,
            },
            "release": {
                "verifier": self.broker.leader_id,
                "holder": "prior-holder",
                "evidence": "independent-release",
                "passed": True,
            },
        }
        # Owner input is a file; the holder supplies no proof or callback.
        self.source = Path(self.temp.name) / "startup.json"
        self.publish()
        self.broker.startup_snapshot = self.read_proof

    def publish(self):
        self.source.write_text(json.dumps(self.proof))
        self.source.chmod(0o600)

    def read_proof(self):
        reader = getattr(compose, "startup_reader", None)
        self.assertTrue(callable(reader), "owner startup evidence reader must exist")
        return reader(
            self.source, 60, self.registry, self.broker.leader_id, clock=lambda: self.now
        )()

    def start(self, **changes):
        request = dict(
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
            run_id="run-1",
            purpose="CHE-fixture",
            device_id=self.device.device_id,
            ttl=300,
            idempotency_key="startup",
            binding=self.binding,
        )
        request.update(changes)
        server = ControlServer.__new__(ControlServer)
        server.broker = self.broker
        return server.dispatch({"op": "start", "args": request})

    def test_owner_commits_starting_lease_with_run_and_fixed_identity(self):
        try:
            got = self.start()
        except Refused as exc:
            self.fail(f"valid owner startup admission refused: {exc.code}")
        self.assertEqual((got["state"], got["access_phase"]), ("active", "starting"))
        self.assertEqual(got["devices"][0]["device_id"], self.device.device_id)
        self.assertEqual(got["startup"]["grant"]["reference"], "startup-grant")
        self.assertEqual(got["startup"]["device"]["console_port"], self.device.console_port)
        for key in ("pid", "process_start", "boot_id", "agent_id", "issue_id"):
            self.assertEqual(got["binding"][key], self.record[key])
        self.assertEqual(got["hard_deadline"], self.now + 7200)
        self.assertEqual(got["expires_at"], self.now + 300)

    def test_untrusted_holder_socket_cannot_start_or_supply_a_phase(self):
        server = BrokerServer.__new__(BrokerServer)
        server.broker = self.broker
        for op, args in (("start", {}), ("acquire", {"access_phase": "starting"})):
            self.refused("invalid_request", lambda: server.dispatch({"op": op, "args": args}))

    def test_no_admission_ticket_or_offer_is_startup_authority(self):
        self.broker.startup_snapshot = None
        self.broker.admitted = {self.device.device_id: self.device.aliases[0]}
        self.refused("startup_not_admitted", self.start)
        self.refused(
            "impossible_batch",
            lambda: self.broker.acquire(
                principal="agent",
                run_id="run-1",
                purpose="CHE-fixture",
                pool="avd",
                device_id=self.device.device_id,
                binding=self.binding,
                idempotency_key="ordinary",
            ),
        )

    def test_missing_stale_and_mismatched_authority_inputs_refuse(self):
        original = json.loads(json.dumps(self.proof))
        variants = []
        for key in ("release", "grant", "device", "generation"):
            row = json.loads(json.dumps(original))
            del row[key]
            variants.append(row)
        for key, value in (
            ("passed", False),
            ("verifier", "agent"),
            ("holder", self.broker.leader_id),
        ):
            row = json.loads(json.dumps(original))
            row["release"][key] = value
            variants.append(row)
        for key, value in (("observed_at", self.now - 61), ("expires_at", self.now)):
            row = json.loads(json.dumps(original))
            row["grant"][key] = value
            variants.append(row)
        for key, value in (("generation", 2), ("run_id", "foreign-run")):
            row = json.loads(json.dumps(original))
            row[key] = value
            variants.append(row)
        row = json.loads(json.dumps(original))
        row["device"]["console_port"] += 2
        variants.append(row)
        for row in variants:
            with self.subTest(proof=row):
                self.proof = row
                self.publish()
                self.refused("startup_not_admitted", self.start)

    def test_claim_sources_and_guard_and_live_managed_binding_are_required(self):
        for writer in ("android", "artemis"):
            prior = self.broker.admission_sources[writer]
            self.broker.admission_sources[writer] = lambda: [self.device.aliases[0]]
            self.refused("device_busy", self.start)
            self.broker.admission_sources[writer] = prior
        self.broker.admission_sources["android"] = lambda: (_ for _ in ()).throw(
            ValueError("stale or incomplete census")
        )
        self.refused("admission_unavailable", self.start)
        self.broker.admission_sources["android"] = lambda: []
        self.record_path.unlink()
        self.refused("run_not_live", self.start)

    def test_alias_race_has_one_lease_and_count_two_is_impossible(self):
        self.refused("impossible_batch", lambda: self.start(count=2))
        barrier = threading.Barrier(2)

        def race(alias):
            barrier.wait()
            try:
                return self.start(device_id=alias, idempotency_key=alias)
            except Refused as exc:
                self.assertIn(exc.code, ("run_already_holds", "device_busy"))

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race, (self.device.device_id, self.device.aliases[0])))
        self.assertEqual(sum(row is not None for row in results), 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_groups").fetchone()[0], 1)

    def test_distinct_live_runs_racing_for_aliases_share_one_capacity_slot(self):
        binding_id = "b" * 64
        second = dict(self.record, binding_id=binding_id, task_id="run-2", issue_id="CHE-second")
        record_path = self.directory / (binding_id + ".json")
        record_path.write_text(json.dumps(second))
        record_path.chmod(0o600)
        second_source = self.source.with_name("second.json")
        second_source.write_text(
            json.dumps(dict(self.proof, run_id="run-2", issue_id="CHE-second"))
        )
        second_source.chmod(0o600)
        local = threading.local()
        self.broker.startup_snapshot = lambda: compose.startup_reader(
            local.source, 60, self.registry, self.broker.leader_id, clock=lambda: self.now
        )()
        barrier = threading.Barrier(2)

        def race(n):
            local.source = second_source if n else self.source
            barrier.wait()
            try:
                return self.start(
                    device_id=self.device.aliases[n],
                    run_id=f"run-{n + 1}",
                    issue_id="CHE-second" if n else "CHE-fixture",
                    binding={"mode": "managed", "binding_id": binding_id} if n else self.binding,
                )
            except Refused as exc:
                self.assertEqual(exc.code, "device_busy")

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(race, (0, 1)))
        self.assertEqual(sum(row is not None for row in results), 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_groups").fetchone()[0], 1)

    def test_commit_hook_and_input_io_are_outside_writer_transaction_and_retry_is_stable(self):
        observations = []
        reader = self.read_proof

        def write_probe():
            with sqlite3.connect(self.path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")

        def source():
            write_probe()
            return reader()

        def hook(name):
            if name == "startup:committed":
                write_probe()
                with sqlite3.connect(self.path) as conn:
                    observations.append(
                        conn.execute(
                            "SELECT count(*) FROM broker_groups g JOIN broker_bindings b USING(lease_id) "
                            "JOIN broker_access a USING(lease_id) WHERE g.state='active' "
                            "AND a.phase='starting'"
                        ).fetchone()[0]
                    )
                raise ConnectionError("uncertain response")

        self.broker.startup_snapshot = source
        self.broker.boundary = hook
        with self.assertRaises(ConnectionError):
            self.start()
        self.broker.boundary = lambda _: None
        first = self.start(device_id=self.device.aliases[0])
        self.now += 20
        self.assertEqual(first, self.start())
        self.assertEqual(observations, [1])
        self.refused(
            "startup_launch_unavailable",
            lambda: self.broker.launch_startup(
                lease_id=first["lease_id"], principal="agent", run_id="run-1"
            ),
        )

    def test_work_mutations_and_renew_are_denied_and_restart_retains_occupancy(self):
        got = self.start()
        owner = dict(lease_id=got["lease_id"], principal="agent", run_id="run-1")
        self.refused(
            "access_phase_denied",
            lambda: self.broker.record_change(
                **owner,
                change={
                    "kind": "app",
                    "device_id": self.device.device_id,
                    "package": "example.app",
                    "installed_by_run": True,
                    "allowed_remove": True,
                },
            ),
        )
        self.now += 250
        self.refused(
            "access_phase_denied",
            lambda: self.broker.renew(**owner, ttl=300, idempotency_key="renew"),
        )
        self.broker.restart()
        self.assertEqual(self.broker.status(**owner)["state"], "fencing")
        self.refused("restart_reconcile", lambda: self.start(idempotency_key="new"))

    def test_real_ledger_requires_complete_fresh_census_and_preserves_foreign_claim(self):
        guard = compose.FileGuard(Path(self.temp.name) / "claims.guard")
        locks = Path(self.temp.name) / "locks"
        locks.mkdir()
        ledger = ClaimLedger(self.path, guard, self.registry, locks, clock=lambda: self.now)
        self.broker.admission_guard = guard
        self.broker.admission_sources = {
            "android": ledger.read,
            "artemis": compose.artemis_locks_reader(locks),
        }
        self.refused("admission_unavailable", self.start)
        ledger.census(
            generation=1,
            observed_at=self.now,
            complete=True,
            writers=["android", "artemis"],
            claims=[],
        )
        claim = ledger.claim(
            self.device.aliases[0], owner="foreign", run_id="other", reference="claim-authority"
        )
        self.refused("device_busy", self.start)
        ledger.release(claim, owner="foreign", run_id="other")
        (locks / "artemis-device-fixture.lock").write_text(
            json.dumps({"device_id": self.device.aliases[0]})
        )
        self.refused("device_busy", self.start)
        (locks / "artemis-device-fixture.lock").unlink()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE android_claim_source SET complete=0")
        self.refused("admission_unavailable", self.start)
        ledger.census(
            generation=1,
            observed_at=self.now,
            complete=True,
            writers=["android", "artemis"],
            claims=[],
        )
        self.now += 61
        self.refused("admission_unavailable", self.start)

    def test_startup_queue_offer_requires_new_managed_run_and_fresh_owner_grant(self):
        holder = self.start()
        self.record.update(task_id="run-2", issue_id="CHE-queued")
        self.write_record()
        self.proof.update(run_id="run-2", issue_id="CHE-queued")
        self.publish()
        queued = self.refused(
            "device_busy",
            lambda: self.start(run_id="run-2", issue_id="CHE-queued", idempotency_key="queued"),
        )
        ticket = queued.details["ticket"]
        self.broker.park(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-queued",
            run_id="run-2",
        )
        self.broker.release(lease_id=holder["lease_id"], principal="agent", run_id="run-1")
        self.broker.recovery(holder["lease_id"], "fencing", "cleaning")
        self.broker.recovery(holder["lease_id"], "cleaning", "verifying")
        self.broker.verify_release(
            holder["lease_id"],
            verifier=self.broker.leader_id,
            evidence="independent fixture release",
            passed=True,
        )
        self.record.update(task_id="run-3")
        self.write_record()
        request = dict(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-queued",
            run_id="run-3",
            binding=self.binding,
            idempotency_key="claim",
        )
        self.refused("access_phase_denied", lambda: self.broker.claim(**request))
        self.refused("startup_not_admitted", lambda: self.broker.claim_startup(**request))
        self.proof["run_id"] = "run-3"
        self.publish()
        got = self.broker.claim_startup(**request)
        self.assertEqual((got["state"], got["access_phase"]), ("active", "starting"))
        self.assertEqual(got, self.broker.claim_startup(**request))
        self.refused(
            "startup_launch_unavailable",
            lambda: self.broker.launch_startup(
                lease_id=ticket["ticket_id"], principal="agent", run_id="run-3"
            ),
        )


if __name__ == "__main__":
    unittest.main()
