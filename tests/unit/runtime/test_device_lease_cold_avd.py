"""CHE-9719: cold-AVD admission with an unleased recovery. Synthetic fixtures only: no ADB,
no emulator, no live broker."""

from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_launch as launch_fixtures
from broker import DeviceRegistry

TARGET = "x99_api35_medium_phone"


class ColdAvdTests(unittest.TestCase):
    setUpFixtures = launch_fixtures.LaunchTests.setUpFixtures
    new_broker = launch_fixtures.LaunchTests.new_broker
    write_record = launch_fixtures.LaunchTests.write_record
    refused = launch_fixtures.LaunchTests.refused
    publish = launch_fixtures.LaunchTests.publish
    read_proof = launch_fixtures.LaunchTests.read_proof
    start = launch_fixtures.LaunchTests.start
    launch = launch_fixtures.LaunchTests.launch
    owner = launch_fixtures.LaunchTests.owner
    recover_after_restart = launch_fixtures.LaunchTests.recover_after_restart
    phase_grant = launch_fixtures.LaunchTests.phase_grant

    def setUp(self):
        launch_fixtures.LaunchTests.setUp(self)
        self.unleased()

    def unleased(self, **changes):
        # The leader reconciles state that has no broker lease. There is no former holder.
        release = {
            "verifier": self.broker.leader_id,
            "unleased": True,
            "evidence": "leader-unleased-reconciliation",
            "passed": True,
        }
        release.update(changes)
        self.proof["release"] = release
        self.publish()

    def audit(self):
        with sqlite3.connect(self.path) as conn:
            try:
                return [json.loads(p) for (p,) in conn.execute("SELECT payload FROM broker_audit")]
            except sqlite3.OperationalError:  # no lease was ever committed
                return []

    def test_unleased_release_commits_an_audited_start_and_invents_no_lease(self):
        group = self.start()
        self.assertEqual((group["state"], group["access_phase"]), ("active", "starting"))
        self.assertNotIn("holder", group["startup"]["release"])
        with sqlite3.connect(self.path) as conn:
            leases = [r[0] for r in conn.execute("SELECT lease_id FROM broker_groups")]
        self.assertEqual(leases, [group["lease_id"]])
        events = [e for e in self.audit() if e["event"] == "unleased_recovery"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["lease_id"], group["lease_id"])
        self.assertEqual(events[0]["device_id"], self.device.device_id)
        self.assertEqual(events[0]["evidence"], "leader-unleased-reconciliation")
        self.assertEqual(events[0]["verifier"], self.broker.leader_id)
        # An idempotent retry replays the lease and writes no second recovery event.
        self.assertEqual(self.start()["lease_id"], group["lease_id"])
        self.assertEqual(len([e for e in self.audit() if e["event"] == "unleased_recovery"]), 1)

    def test_malformed_or_unverified_unleased_release_refuses(self):
        leader = self.broker.leader_id
        bad = (
            {"unleased": False},
            {"unleased": "yes"},
            {"passed": False},
            {"verifier": "agent"},
            {"verifier": "someone-else"},
            {"evidence": " "},
            {"holder": "invented-holder"},
        )
        for change in bad:
            with self.subTest(change=change):
                self.unleased(verifier=leader)
                self.unleased(**change)
                self.refused("startup_not_admitted", self.start)
        self.assertEqual(self.audit(), [])

    def test_conflicting_ownership_refuses_before_any_lease(self):
        for writer in ("android", "artemis"):
            prior = self.broker.admission_sources[writer]
            self.broker.admission_sources[writer] = lambda: [self.device.aliases[0]]
            self.refused("device_busy", self.start)
            self.broker.admission_sources[writer] = prior
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_groups").fetchone(), (0,))
        self.assertEqual([e for e in self.audit() if e["event"] == "unleased_recovery"], [])

    def test_existing_leased_state_cannot_be_bypassed_with_an_unleased_release(self):
        group = self.start()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE broker_groups SET state='quarantine'")
        # The same run is refused by its run budget; the occupied slot stays held.
        self.refused("run_already_holds", lambda: self.start(idempotency_key="again"))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT lease_id,state FROM broker_groups").fetchall(),
                [(group["lease_id"], "quarantine")],
            )

    def test_missing_launcher_binding_refuses(self):
        self.record_path.unlink()
        self.refused("run_not_live", self.start)
        self.assertEqual(self.audit(), [])

    def test_failed_boot_never_releases_and_never_spawns_twice(self):
        group = self.group = self.start()
        self.io.error = ConnectionError("emulator died during boot")
        with self.assertRaises(ConnectionError):
            self.launch()
        self.io.error = None
        self.launch()  # replays the journal; it never executes a second child
        self.assertEqual(self.io.calls.count("execute"), 1)
        self.assertEqual(self.broker.status(**self.owner())["state"], "active")
        self.assertEqual(group["lease_id"], self.broker.status(**self.owner())["lease_id"])

    def test_failed_cleanup_quarantines_and_keeps_the_slot_held(self):
        self.group = self.start()
        self.launch()
        replacement = dict(self.io.child, process_start="reused")
        live = {row["pid"]: row for row in (self.io.worker, replacement)}
        self.assertEqual(self.recover_after_restart(live), [])
        self.assertEqual(self.broker.status(**self.owner())["state"], "quarantine")
        self.refused("run_already_holds", lambda: self.start(idempotency_key="again"))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_groups").fetchone(), (1,))

    def test_target_is_its_own_identity_without_port_aliasing(self):
        row = replace(
            self.device,
            device_id=TARGET,
            avd_name=TARGET,
            avd_data_path=f"/fixture/android/avd/{TARGET}.avd",
            avd_definition_path=f"/fixture/android/avd/{TARGET}.ini",
            console_port=5556,
            adb_port=5557,
            aliases=("emulator-5556", "127.0.0.1:5557"),
        )
        registry = DeviceRegistry([row])
        self.assertEqual(registry.resolve("emulator-5556").device_id, TARGET)
        self.assertEqual(registry.resolve("127.0.0.1:5557").device_id, TARGET)
        self.assertIsNone(registry.resolve("emulator-5570"))
        self.assertIsNone(registry.resolve("che2081_author_api35_20261026"))
        self.assertEqual(asdict(registry.resolve(TARGET))["identity_confirmed"], False)


if __name__ == "__main__":
    unittest.main()
