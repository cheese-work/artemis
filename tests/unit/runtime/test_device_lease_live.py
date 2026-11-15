"""Live serve composition, owner producers, holder/control separation; fixture I/O."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_compose as fixtures
import compose
import daemon
from broker import Broker, Refused


class LiveTests(unittest.TestCase):
    setUp = fixtures.ComposeTests.setUp
    write_json = fixtures.ComposeTests.write_json
    write_admitted = fixtures.ComposeTests.write_admitted
    config_file = fixtures.ComposeTests.config_file
    take = fixtures.ComposeTests.take

    def test_legacy_unverified_mirror_and_plugin_are_not_live_composition(self):
        self.config["android_claims"] = {
            "path": str(self.root / "android.json"),
            "max_age_seconds": 60,
        }
        self.config["recovery"]["adapter"] = "test_device_lease_compose:FakeRecovery"
        with self.assertRaises(compose.CompositionError):
            compose.compose(self.config_file(), fixtures.registry())

    def config_live(self):
        self.config["android_claims"] = {"source": "ledger", "max_age_seconds": 60}
        self.config["recovery"]["adapter"] = "owned"
        return self.config_file()

    def live(self):
        config = self.config_live()
        composed = compose.compose(
            config, fixtures.registry(), db_path=self.root / "leases.sqlite3"
        )
        broker = Broker(self.root / "leases.sqlite3", fixtures.registry(), **composed["broker"])
        broker.inventory()
        return broker, composed

    def census(self, composed):
        return composed["ledger"].census(
            generation=1,
            observed_at=time.time(),
            complete=True,
            writers=["android", "artemis"],
            claims=[],
        )

    def test_live_inventory_requires_census_and_revoked_grant_excludes_device(self):
        broker, composed = self.live()
        self.assertFalse(broker.inventory()["admission_available"])
        self.census(composed)
        self.assertTrue(broker.inventory()["admission_available"])
        self.assertEqual(
            {d["device_id"] for d in broker.inventory()["devices"] if d["eligible"]},
            {"rig-1", "rig-2"},
        )
        self.write_admitted({"rig-2": "rig-2"})
        inventory = broker.inventory()
        self.assertEqual([d["device_id"] for d in inventory["devices"] if d["eligible"]], ["rig-2"])

    def test_owner_cli_writes_readiness_census_claim_and_owned_release(self):
        config = self.config_live()
        reg = self.write_json(
            "registry.json",
            [
                {
                    "device_id": "rig-1",
                    "aliases": ["usb-rig-1"],
                    "restricted": False,
                    "identity_confirmed": True,
                }
            ],
        )
        observations = self.write_json(
            "observations.json", json.loads((self.root / "admitted.json").read_text())
        )
        census = self.write_json(
            "census.json",
            {
                "generation": 1,
                "observed_at": time.time(),
                "complete": True,
                "writers": ["android", "artemis"],
                "claims": [],
            },
        )
        flags = [
            "--composition",
            str(config),
            "--registry",
            str(reg),
            "--state-dir",
            str(self.root / "state"),
        ]

        def run(op, *args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = daemon.main([op, *flags, *args])
            self.assertEqual(code, 0, op)
            return json.loads(out.getvalue())

        run("write-readiness", "--input", str(observations))
        run("census-android-claims", "--input", str(census))
        claim = run(
            "claim-android",
            "--device-id",
            "usb-rig-1",
            "--owner",
            "android",
            "--run-id",
            "r",
            "--reference",
            "CHE-fixture",
        )
        self.assertTrue(claim["claim_id"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                daemon.main(
                    [
                        "release-android",
                        *flags,
                        "--claim-id",
                        claim["claim_id"],
                        "--owner",
                        "other",
                        "--run-id",
                        "r",
                    ]
                ),
                2,
            )
        run(
            "release-android",
            "--claim-id",
            claim["claim_id"],
            "--owner",
            "android",
            "--run-id",
            "r",
        )
        # A stale producer must not replace a newer readiness generation.
        payload = json.loads(observations.read_text())
        payload["generation"] = 2
        observations.write_text(json.dumps(payload))
        run("write-readiness", "--input", str(observations))
        payload["generation"] = 1
        observations.write_text(json.dumps(payload))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                daemon.main(["write-readiness", *flags, "--input", str(observations)]), 2
            )

    def test_serve_uses_same_database_inventory_and_leader_control(self):
        config = self.config_live()
        state = self.root / "state"
        state.mkdir(mode=0o700)
        reg = self.write_json(
            "registry.json",
            [
                {
                    "device_id": "rig-1",
                    "aliases": ["usb-rig-1"],
                    "restricted": False,
                    "identity_confirmed": True,
                }
            ],
        )
        # Census registry must match the actual serve inventory exactly.
        composed = compose.compose(
            config, daemon.DeviceRegistry.load(reg), db_path=state / "leases.sqlite3"
        )
        self.census(composed)
        observed = []

        def exercise(server):
            observed.append(server.broker.inventory()["admission_available"])
            identity = {"mode": "local", "pid": 1234, "process_start": "42", "boot_id": "fixture"}
            server.broker.run_probe = lambda binding: True
            group = self.take(server.broker, device_id="usb-rig-1", binding=identity)
            change = {
                "kind": "work",
                "device_id": "rig-1",
                "complete": True,
                "processes": [identity],
                "forwards": [],
            }
            with self.assertRaises(Refused):
                server.dispatch(
                    {
                        "op": "record-change",
                        "args": {
                            "lease_id": group["lease_id"],
                            "principal": "agent",
                            "run_id": "run-1",
                            "change": change,
                        },
                    }
                )
            reply = daemon.rpc(
                self.root / "leader" / "control.sock",
                {
                    "op": "record-change",
                    "args": {
                        "lease_id": group["lease_id"],
                        "principal": "agent",
                        "run_id": "run-1",
                        "change": change,
                    },
                },
            )
            self.assertTrue(reply["recorded"])
            server.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")
            adapter = server.broker.recovery_io
            from owned_recovery import OwnedRecovery

            self.assertIsInstance(adapter, OwnedRecovery)
            live = {identity["pid"]: identity}

            def stop(identity, timeout):
                live.pop(identity["pid"])
                return True

            with (
                patch.object(adapter, "_daemon_pid", return_value=999),
                patch.object(adapter, "_descendants", return_value=set()),
                patch.object(adapter, "_identity", side_effect=lambda pid: live.get(pid)),
                patch.object(adapter, "_stop", side_effect=stop),
                patch.object(adapter, "_adb", return_value=""),
            ):
                server.broker.maintain()
            reply = daemon.rpc(
                self.root / "leader" / "control.sock",
                {
                    "op": "verify-release",
                    "args": {
                        "lease_id": group["lease_id"],
                        "verifier": fixtures.LEADER,
                        "evidence": "CHE-fixture",
                        "passed": True,
                    },
                },
            )
            self.assertEqual(reply["state"], "released")

        with patch.object(daemon.BrokerServer, "serve_forever", exercise):
            code = daemon.main(
                [
                    "serve",
                    "--state-dir",
                    str(state),
                    "--registry",
                    str(reg),
                    "--composition",
                    str(config),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(observed, [True])
        self.assertFalse((state / "broker.sock").exists())


if __name__ == "__main__":
    unittest.main()
