"""Physical-device claim-to-lease transfer and SDK lock (CHE-10283). Offline: no ADB, no device."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_managed_runs as managed_fixture
import compose
import daemon
from broker import Refused, process_identity
from daemon import BrokerServer, ControlServer
from device_lease.claims import ClaimLedger
from registry import AdmissionSnapshot


class PhysicalTransferTests(unittest.TestCase):
    setUpFixtures = managed_fixture.ManagedRunTests.setUpFixtures
    new_broker = managed_fixture.ManagedRunTests.new_broker
    take = managed_fixture.ManagedRunTests.take
    write_record = managed_fixture.ManagedRunTests.write_record
    refused = managed_fixture.ManagedRunTests.refused

    def setUp(self):
        managed_fixture.ManagedRunTests.setUp(self)
        # The test process stands in for the managed run so the SDK peer is its descendant.
        self.record.update({k: v for k, v in process_identity(os.getpid()).items() if k != "mode"})
        self.write_record()
        self.locks = Path(self.temp.name) / "locks"
        self.locks.mkdir()
        self.guard = compose.FileGuard(Path(self.temp.name) / "claims.guard")
        self.ledger = ClaimLedger(
            self.path, self.guard, self.registry, self.locks, clock=lambda: self.now
        )
        self.broker.admission_guard = self.guard
        self.broker.admission_sources = {
            "artemis": compose.artemis_locks_reader(self.locks),
            "android": self.ledger.read,
        }
        self.window = 900
        self.broker.admission_snapshot = self.readiness
        self.broker.ensure_schema()
        self.census(7)
        self.claim_id = self.ledger.claim(
            "a", owner="agent", run_id="run-1", reference="CHE-fixture"
        )
        self.consent = dict(
            claim_id=self.claim_id,
            owner="agent",
            run_id="run-1",
            census_generation=7,
            reference="CHE-fixture",
            device_id="a",
            serial="usb-a",
            agent_id="agent",
            issue_id="CHE-fixture",
            grant_reference="grant-a",
        )

    def readiness(self):
        grant = lambda ref: dict(  # noqa: E731
            reference=ref, observed_at=self.now, expires_at=self.now + self.window
        )
        return AdmissionSnapshot(
            {"a": "usb-a", "b": "usb-b", "root-a": "root-a"},
            {"a": grant("grant-a"), "b": grant("grant-b")} if self.window > 0 else {},
        )

    def census(self, generation, claims=None):
        self.ledger.census(
            generation=generation,
            observed_at=self.now,
            complete=True,
            writers=["android", "artemis"],
            claims=claims
            if claims is not None
            else [
                dict(
                    claim_id=c["claim_id"],
                    device_id=c["device_id"],
                    owner=c["owner"],
                    run_id=c["run_id"],
                    reference=c["reference"],
                )
                for c in self.ledger.snapshot()["claims"]
            ],
        )

    def transfer(self, **changes):
        request = dict(
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
            run_id="run-1",
            device_id="a",
            binding=self.binding,
            consent=self.consent,
            actor="operator",
            actor_run_id="operator-run",
            ttl=300,
            idempotency_key="transfer",
        )
        request.update(changes)
        server = ControlServer.__new__(ControlServer)
        server.broker = self.broker
        return server.dispatch({"op": "transfer-physical", "args": request})

    def claims(self):
        return self.ledger.snapshot()["claims"]

    def groups(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT count(*) FROM broker_groups").fetchone()[0]

    def receipts(self, event="claim_transfer"):
        with sqlite3.connect(self.path) as conn:
            rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM broker_audit")]
        return [r for r in rows if r["event"] == event]

    def assertUntouched(self, before):
        self.assertEqual(self.claims(), before)
        self.assertEqual(self.groups(), 0)

    # -- transfer -------------------------------------------------------------------------

    def test_physical_transfer_moves_the_exact_claim_into_one_attributed_lease(self):
        other = self.ledger.claim("b", owner="agent", run_id="run-1", reference="other")
        before = self.claims()
        group = self.transfer()
        self.assertEqual((group["state"], group["access_phase"]), ("active", "work"))
        self.assertEqual(
            [(d["device_id"], d["endpoint"]) for d in group["devices"]], [("a", "usb-a")]
        )
        self.assertEqual(group["expires_at"], self.now + 300)
        self.assertEqual(group["grants"][0]["reference"], "grant-a")
        # Only the exact claim moved; a claim on another device is untouched.
        self.assertEqual([c["claim_id"] for c in self.claims()], [other])
        with sqlite3.connect(self.path) as conn:
            moved, consent = conn.execute(
                "SELECT claim,consent FROM android_claim_handoffs WHERE lease_id=?",
                (group["lease_id"],),
            ).fetchone()
        self.assertEqual(
            json.loads(moved), next(c for c in before if c["claim_id"] == self.claim_id)
        )
        self.assertEqual(json.loads(consent), self.consent)
        (receipt,) = self.receipts()
        self.assertEqual(
            {
                k: receipt[k]
                for k in ("agent", "run_id", "claim_id", "actor", "actor_run_id", "actor_verified")
            },
            dict(
                agent="agent",
                run_id="run-1",
                claim_id=self.claim_id,
                actor="operator",
                actor_run_id="operator-run",
                actor_verified=False,
            ),
        )
        self.assertEqual(receipt["lease_id"], group["lease_id"])

    def test_transfer_is_refused_unless_every_bound_field_matches(self):
        before = self.claims()
        for label, changes in {
            "consent owner": dict(consent=self.consent | dict(owner="foreign")),
            "consent run": dict(consent=self.consent | dict(run_id="run-2")),
            "claim id": dict(consent=self.consent | dict(claim_id="0" * 32)),
            "serial": dict(consent=self.consent | dict(serial="usb-b")),
            "consent device": dict(consent=self.consent | dict(device_id="b")),
            "consent issue": dict(consent=self.consent | dict(issue_id="CHE-other")),
            "claim reference": dict(consent=self.consent | dict(reference="CHE-other")),
            "window reference": dict(consent=self.consent | dict(grant_reference="grant-b")),
            "census generation": dict(consent=self.consent | dict(census_generation=6)),
            "extra consent field": dict(consent=self.consent | dict(extra="x")),
            "request issue": dict(issue_id="CHE-other"),
            "foreign principal": dict(principal="foreign", agent_id="foreign"),
            "wrong run": dict(run_id="run-2"),
            "no actor": dict(actor=""),
            "no actor run": dict(actor_run_id=" "),
            "avd pool device": dict(device_id="root-a"),
        }.items():
            with self.subTest(label), self.assertRaises(Refused):
                self.transfer(**changes)
            self.assertUntouched(before)
        self.assertEqual(self.receipts(), [])

    def test_expired_window_stale_census_and_claim_owner_change_refuse(self):
        before = self.claims()
        self.window = 0  # readiness no longer carries a live admission window
        with self.assertRaises(Refused):
            self.transfer(idempotency_key="expired")
        self.window = 900
        self.assertUntouched(before)
        self.now += 61  # census older than its maximum age
        with self.assertRaises(Refused):
            self.transfer(idempotency_key="stale")
        self.assertUntouched(before)
        self.census(8)  # a newer generation invalidates consent bound to 7
        with self.assertRaises(Refused) as caught:
            self.transfer(idempotency_key="newer")
        self.assertEqual(caught.exception.code, "claim_handoff_denied")
        self.assertUntouched(before)

    def test_holder_socket_cannot_transfer(self):
        server = BrokerServer.__new__(BrokerServer)
        server.broker = self.broker
        self.refused(
            "invalid_request",
            lambda: server.dispatch(
                {"op": "transfer-physical", "args": dict(consent=self.consent)}
            ),
        )

    def test_rollback_leaves_claim_and_retry_commits_once_with_one_receipt(self):
        before = self.claims()

        def interrupt(name):
            if name == "acquire:device":
                raise RuntimeError("fixture rollback")

        self.broker.boundary = interrupt
        with self.assertRaisesRegex(RuntimeError, "fixture rollback"):
            self.transfer()
        self.assertUntouched(before)
        self.assertEqual(self.receipts(), [])
        self.broker.boundary = lambda name: None
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(lambda unused: self.transfer(), range(2))
        self.assertEqual(first, second)
        self.assertEqual(self.groups(), 1)
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.receipts()), 1)
        # A different key cannot transfer the claim twice.
        with self.assertRaises(Refused):
            self.transfer(idempotency_key="again")
        self.assertEqual(self.groups(), 1)

    def test_competing_acquire_ticket_and_sdk_lock_never_drop_the_claim(self):
        before = self.claims()
        # Ordinary acquire: a claimed device is never granted, even to the claim owner.
        busy = self.refused(
            "device_busy",
            lambda: self.take(device_id="a", run_id="run-9", idempotency_key="ord"),
        )
        self.assertIsNotNone(busy.details["ticket"])
        self.assertEqual(self.claims(), before)
        self.broker.cancel_ticket(
            ticket_id=busy.details["ticket"]["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
        )
        # A queued ticket that could use the device keeps its FIFO place.
        self.take(device_id="b", run_id="run-7", idempotency_key="hold-b")
        self.refused(
            "device_busy", lambda: self.take(pool="rig", run_id="run-8", idempotency_key="q")
        )
        self.refused("device_busy", self.transfer)
        self.assertEqual(self.claims(), before)
        self.assertEqual(self.groups(), 1)  # only the unrelated lease on device b
        self.assertEqual(self.receipts(), [])

    def test_foreign_sdk_lock_blocks_transfer(self):
        before = self.claims()
        (self.locks / "artemis-device-usb-a.lock").write_text(
            json.dumps(dict(pid=1, token="f" * 32, device_id="usb-a"))
        )
        with self.assertRaises(Refused):
            self.transfer()
        self.assertUntouched(before)

    # -- SDK execution lock ---------------------------------------------------------------

    def sdk(self):
        from artemis.runtime.device_lock import DeviceExecutionLock

        self.server = BrokerServer(Path(self.temp.name) / "holder", self.broker)
        self.server.timeout = 2
        self.addCleanup(self.server.server_close)
        self.group = self.transfer()
        self.handoff = dict(
            socket=str(self.server.socket_path),
            lease_id=self.group["lease_id"],
            device_id="a",
            issue_id="CHE-fixture",
            generation=7,
            phase="work",
        )
        self.env = dict(
            ARTEMIS_DEVICE_ADMISSION_GUARD=str(self.guard.path),
            ARTEMIS_DEVICE_CLAIMS_DB=str(self.path),
            MULTICA_AGENT_ID="agent",
            MULTICA_TASK_ID="run-1",
            MULTICA_RUN_BINDING_ID=self.binding_id,
        )
        with patch("artemis.runtime.device_lock.get_temp_dir", return_value=self.locks):
            self.lock = DeviceExecutionLock("usb-a")

    def attempt(self, **changes):
        with (
            patch.dict(
                os.environ,
                self.env | {"ARTEMIS_DEVICE_LEASE_HANDOFF": json.dumps(self.handoff | changes)},
            ),
            ThreadPoolExecutor(1) as pool,
        ):
            served = pool.submit(self.server.handle_request)
            result = self.lock._try_acquire_owner_lock()
            served.result(timeout=5)
            return result

    def test_sdk_lock_under_the_physical_lease_and_fencing(self):
        self.sdk()
        for changes in (
            {"device_id": "usb-a"},
            {"issue_id": "foreign"},
            {"generation": 6},
            {"phase": "qualification"},
            {"lease_id": "0" * 32},
        ):
            with self.subTest(**changes):
                self.assertFalse(self.attempt(**changes))
        self.env["MULTICA_TASK_ID"] = "foreign"
        self.assertFalse(self.attempt())
        self.env["MULTICA_TASK_ID"] = "run-1"
        self.assertFalse(self.lock.path.exists())
        # Without a handoff the SDK is still refused: a lease alone grants no SDK access.
        with patch.dict(os.environ, self.env):
            self.assertFalse(self.lock._try_acquire_owner_lock())
        self.assertTrue(self.attempt(), "reviewed physical lease holder was refused")
        self.assertTrue(self.attempt(), "exact retry did not reuse the execution lock")
        self.assertEqual(self.broker.status(**self.owner())["access_phase"], "work")
        # Fencing: once the run is gone the lease fences and the SDK revalidation fails.
        self.record_path.unlink()
        self.assertFalse(self.attempt())
        self.assertEqual(self.broker.status(**self.owner())["state"], "fencing")

    def owner(self):
        return dict(lease_id=self.group["lease_id"], principal="agent", run_id="run-1")

    def test_second_sdk_holder_is_refused_while_the_first_holds_the_lock(self):
        from artemis.runtime.device_lock import DeviceExecutionLock

        self.sdk()
        self.assertTrue(self.attempt())
        with patch("artemis.runtime.device_lock.get_temp_dir", return_value=self.locks):
            rival = DeviceExecutionLock("usb-a")
        self.lock, first = rival, self.lock
        self.assertFalse(self.attempt())
        self.assertTrue(first.path.exists())

    def verifying(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE broker_groups SET state='verifying' WHERE lease_id=?",
                (self.group["lease_id"],),
            )

    def release(self):
        return self.broker.verify_release(
            self.group["lease_id"],
            verifier=self.broker.leader_id,
            evidence="independent",
            passed=True,
        )

    def test_release_cleans_a_dead_holders_lock_but_never_a_live_one(self):
        self.sdk()
        self.assertTrue(self.attempt())
        path = self.lock.path
        self.verifying()
        self.refused("execution_lock_busy", self.release)  # the holder process is alive
        self.assertTrue(path.exists())
        with patch("broker.process_identity", side_effect=ProcessLookupError):
            self.assertEqual(self.release()["state"], "released")
        self.assertFalse(path.exists())

    def test_release_keeps_a_foreign_lock_it_did_not_install(self):
        self.sdk()
        self.assertTrue(self.attempt())
        path = self.lock.path
        path.write_text(json.dumps(dict(pid=1, token="e" * 32, device_id="usb-a")))
        self.verifying()
        with patch("broker.process_identity", side_effect=ProcessLookupError):
            self.assertEqual(self.release()["state"], "released")
        self.assertTrue(path.exists())

    def paused_transfer(self):
        """Pause the transfer inside its transaction, under the admission guard."""
        inside, resume = threading.Event(), threading.Event()

        def pause(name):
            if name == "acquire:device":
                inside.set()
                self.assertTrue(resume.wait(10), "fixture never resumed the transfer")

        self.broker.boundary = pause
        pool = ThreadPoolExecutor(4)
        self.addCleanup(pool.shutdown, wait=True)
        self.addCleanup(resume.set)
        transfer = pool.submit(self.transfer)
        self.assertTrue(inside.wait(10), "transfer never reached its transaction")
        return pool, transfer, resume

    def test_acquire_racing_a_paused_transfer_waits_then_never_takes_the_device(self):
        pool, transfer, resume = self.paused_transfer()
        racer = pool.submit(
            lambda: self.take(device_id="a", run_id="run-9", idempotency_key="race")
        )
        threading.Event().wait(0.3)
        self.assertFalse(racer.done(), "acquire ran inside the transfer transaction")
        resume.set()
        group = transfer.result(timeout=10)
        with self.assertRaises(Refused) as caught:
            racer.result(timeout=10)
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.groups(), 1)
        self.assertEqual(len(self.receipts()), 1)
        self.assertEqual(
            self.broker.status(lease_id=group["lease_id"], principal="agent", run_id="run-1")[
                "state"
            ],
            "active",
        )

    def test_ticket_claim_racing_a_paused_transfer_gets_only_its_own_device(self):
        hold = self.take(device_id="b", run_id="run-7", idempotency_key="hold-b")
        busy = self.refused(
            "device_busy", lambda: self.take(device_id="b", run_id="wait", idempotency_key="q")
        )
        ticket_id = busy.details["ticket"]["ticket_id"]
        self.broker.park(
            ticket_id=ticket_id,
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
            run_id="wait",
        )
        with sqlite3.connect(self.path) as conn:  # the holder's verified release, simulated
            conn.execute(
                "UPDATE broker_groups SET state='released' WHERE lease_id=?", (hold["lease_id"],)
            )
        offered = self.broker.ticket_status(
            ticket_id=ticket_id, principal="agent", agent_id="agent", issue_id="CHE-fixture"
        )
        self.assertEqual(offered["state"], "offered")
        pool, transfer, resume = self.paused_transfer()
        claimed = pool.submit(
            lambda: self.broker.claim(
                ticket_id=ticket_id,
                principal="agent",
                agent_id="agent",
                issue_id="CHE-fixture",
                run_id="successor",
                binding=process_identity(os.getpid()),
                idempotency_key="claim-b",
            )
        )
        threading.Event().wait(0.3)
        self.assertFalse(claimed.done(), "ticket claim ran inside the transfer transaction")
        resume.set()
        group = transfer.result(timeout=10)
        lease = claimed.result(timeout=10)
        self.assertNotEqual(lease["lease_id"], group["lease_id"])
        self.assertEqual([d["device_id"] for d in lease["devices"]], ["b"])
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.receipts()), 1)

    def test_foreign_sdk_lock_writer_racing_a_paused_transfer_waits_for_the_guard(self):
        pool, transfer, resume = self.paused_transfer()
        foreign = self.locks / "artemis-device-usb-a.lock"

        def write_foreign_lock():
            with self.guard:  # Artemis lock writers take this guard before writing.
                foreign.write_text(json.dumps(dict(pid=1, token="f" * 32, device_id="usb-a")))

        racer = pool.submit(write_foreign_lock)
        threading.Event().wait(0.3)
        self.assertFalse(racer.done(), "foreign lock writer ran inside the transfer transaction")
        self.assertFalse(foreign.exists())
        resume.set()
        group = transfer.result(timeout=10)
        racer.result(timeout=10)
        # The transfer won the guard first: the lease exists and the later foreign lock
        # now blocks SDK access under it instead of being replaced.
        self.assertEqual(group["state"], "active")
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.groups(), 1)
        self.assertTrue(foreign.exists())

    def test_release_surfaces_a_cleanup_failure_and_keeps_the_lease(self):
        self.sdk()
        self.assertTrue(self.attempt())
        path = self.lock.path
        self.verifying()
        with (
            patch("broker.process_identity", side_effect=ProcessLookupError),
            patch.object(Path, "unlink", side_effect=PermissionError("fixture")),
        ):
            self.refused("execution_lock_cleanup_failed", self.release)
        self.assertTrue(path.exists())
        self.assertEqual(self.broker.status(**self.owner())["state"], "verifying")
        with patch("broker.process_identity", side_effect=ProcessLookupError):
            self.assertEqual(self.release()["state"], "released")
        self.assertFalse(path.exists())

    # -- CLI ------------------------------------------------------------------------------

    def test_cli_sends_consent_file_and_managed_binding_over_the_control_socket(self):
        consent = Path(self.temp.name) / "consent.json"
        consent.write_text(json.dumps(self.consent))
        consent.chmod(0o600)
        argv = [
            "--control-socket",
            "/control.sock",
            "transfer-physical",
            "--principal",
            "agent",
            "--agent-id",
            "agent",
            "--issue-id",
            "CHE-fixture",
            "--run-id",
            "run-1",
            "--device-id",
            "a",
            "--managed-binding",
            self.binding_id,
            "--consent-file",
            str(consent),
            "--actor",
            "operator",
            "--actor-run-id",
            "operator-run",
            "--idempotency-key",
            "cli",
        ]
        with patch("daemon.rpc", return_value={}) as rpc, redirect_stdout(io.StringIO()):
            self.assertEqual(daemon.main(argv), 0)
        socket, request = rpc.call_args.args
        self.assertEqual(str(socket), "/control.sock")
        self.assertEqual(request["op"], "transfer-physical")
        self.assertEqual(request["args"]["consent"], self.consent)
        self.assertEqual(request["args"]["binding"], self.binding)
        self.assertEqual(request["args"]["actor"], "operator")
        self.assertEqual(request["args"]["ttl"], 1800)
        with self.assertRaises(SystemExit):
            daemon.main([a for a in argv if a not in ("--actor", "operator")])


if __name__ == "__main__":
    unittest.main()
