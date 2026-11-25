"""Owner refresh observes shared ADB and guarded writers without starting ADB."""

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
from broker import Broker


class Wire:
    def __init__(self, payload):
        self.data = io.BytesIO(b"OKAY" + f"{len(payload):04x}".encode() + payload)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def sendall(self, value):
        assert value == b"000ehost:devices-l"

    def recv(self, count):
        return self.data.read(count)


class RefreshTests(unittest.TestCase):
    setUp = fixtures.ComposeTests.setUp
    write_json = fixtures.ComposeTests.write_json
    write_admitted = fixtures.ComposeTests.write_admitted
    config_file = fixtures.ComposeTests.config_file

    def prepare(self):
        locks = self.root / "artemis/device-locks"
        locks.mkdir(parents=True)
        self.config["artemis_locks_dir"] = str(locks)
        self.composed = compose.compose(
            self.config_file(), fixtures.registry(), db_path=self.root / "leases.sqlite3"
        )
        self.broker = Broker(
            self.root / "leases.sqlite3", fixtures.registry(), **self.composed["broker"]
        )
        self.broker.inventory()
        self.ledger = self.composed["ledger"]
        self.ledger.census(
            generation=1,
            observed_at=time.time(),
            complete=True,
            writers=["android", "artemis"],
            claims=[],
        )
        self.claim = self.ledger.claim(
            "rig-2", owner="other", run_id="retained-run", reference="existing owned claim"
        )
        self.host = {
            "devices": ["rig-1", "rig-2"],
            "reference": "CHE-fixture",
            "expires_at": time.time() + 600,
            "writers": [
                {
                    "unit": "fixture.service",
                    "sdk_lock": str(fixtures.ROOT / "artemis/runtime/device_lock.py"),
                }
            ],
            "isolated_writers": [],
        }
        self.environment = {
            "TMPDIR": str(self.root),
            "ARTEMIS_DEVICE_ADMISSION_GUARD": str(self.composed["guard"].path),
            "ARTEMIS_DEVICE_CLAIMS_DB": str(self.root / "leases.sqlite3"),
        }
        self.registry_rows = [
            dict(
                device_id="rig-1", aliases=["usb-rig-1"], restricted=False, identity_confirmed=True
            ),
            dict(device_id="rig-2", restricted=False, identity_confirmed=True),
            dict(device_id="spare-1", pool="spare"),
        ]

    def refresh(
        self, payload=b"rig-1\tdevice product:fixture\nspare-1\tdevice\n", *, missing=False
    ):
        config = self.write_json("host.json", self.host)
        registry = self.write_json("registry.json", self.registry_rows)
        original = Path.read_bytes
        env = b"\0".join(f"{k}={v}".encode() for k, v in self.environment.items())

        def read(path):
            if str(path) == f"/proc/{os.getpid()}/environ":
                return env
            return original(path)

        with (
            patch("subprocess.check_output", return_value=str(os.getpid())),
            patch.object(Path, "read_bytes", read),
            patch(
                "socket.create_connection",
                return_value=Wire(payload),
                side_effect=ConnectionRefusedError if missing else None,
            ) as connect,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                result = daemon.main(
                    [
                        "refresh",
                        "--composition",
                        str(self.config_file()),
                        "--registry",
                        str(registry),
                        "--state-dir",
                        str(self.root),
                        "--host-config",
                        str(config),
                    ]
                )
            except SystemExit as exc:
                result = exc.code
            if missing:
                connect.assert_called_once_with(("127.0.0.1", 5037), timeout=3)
            return result

    def test_cli_refresh_observes_grants_and_retains_existing_claim(self):
        self.prepare()
        self.assertEqual(self.refresh(), 0, "owner refresh must succeed for a guarded live writer")
        admitted = json.loads((self.root / "admitted.json").read_text())
        self.assertEqual([r["device_id"] for r in admitted["devices"]], ["rig-1"])
        self.assertEqual(self.ledger.snapshot()["claims"][0]["claim_id"], self.claim)
        self.assertTrue(self.broker.inventory()["admission_available"])

    def pool_config(self):
        self.prepare()
        self.host["pool"] = dict(
            name="rig",
            devices=self.host.pop("devices"),
            reference="CHE-9610 standing rig approval",
            expires_at=self.host.pop("expires_at"),
        )
        self.host.pop("reference")

    def test_pool_refresh_admits_all_visible_members_and_retains_owned_claims(self):
        self.pool_config()
        self.assertEqual(self.refresh(b"rig-1\tdevice\nrig-2\tdevice\nspare-1\tdevice\n"), 0)
        rows = json.loads((self.root / "admitted.json").read_text())["devices"]
        self.assertEqual([r["device_id"] for r in rows], ["rig-1", "rig-2"])
        self.assertEqual({r["grant"]["reference"] for r in rows}, {self.host["pool"]["reference"]})
        self.assertEqual(self.ledger.snapshot()["claims"][0]["claim_id"], self.claim)
        # The pool remains configured when a phone disconnects; readiness does not.
        self.assertEqual(self.refresh(b"rig-2\toffline\nrig-1\tdevice\n"), 0)
        self.assertEqual(
            [
                r["device_id"]
                for r in json.loads((self.root / "admitted.json").read_text())["devices"]
            ],
            ["rig-1"],
        )

    def test_standing_pool_has_no_global_expiry_window(self):
        self.pool_config()
        self.host["pool"].pop("expires_at")
        before = time.time()
        self.assertEqual(self.refresh(), 0)
        grant = json.loads((self.root / "admitted.json").read_text())["devices"][0]["grant"]
        self.assertEqual(grant["reference"], self.host["pool"]["reference"])
        self.assertGreaterEqual(grant["expires_at"], before + 7200)
        self.assertLessEqual(grant["expires_at"], time.time() + 7200)

    def test_pool_rejects_restricted_spare_unknown_and_mixed_legacy_configuration(self):
        self.pool_config()
        pool = dict(self.host["pool"])
        for change in (
            {"devices": ["spare-1"]},
            {"devices": ["500c592"]},
            {"devices": ["unknown"]},
            {"name": "spare"},
            {"devices": ["rig-1", "rig-1"]},
        ):
            self.host["pool"] = pool | change
            self.assertEqual(self.refresh(), 2)
            self.assertFalse(self.broker.inventory()["admission_available"])
        self.host["pool"] = pool
        self.host["devices"] = ["rig-1"]
        self.assertEqual(self.refresh(), 2)

    def test_each_pool_lease_records_issue_run_serial_and_authority_independently(self):
        self.pool_config()
        self.ledger.release(self.claim, owner="other", run_id="retained-run")
        self.assertEqual(self.refresh(b"rig-1\tdevice\nrig-2\tdevice\n"), 0)
        groups = [
            self.broker.acquire(
                principal="executor",
                agent_id="executor",
                issue_id=f"CHE-{i}",
                run_id=f"run-{i}",
                purpose="pool fixture",
                ttl=300,
                idempotency_key=f"key-{i}",
            )
            for i in (1, 2)
        ]
        for i, group in enumerate(groups, 1):
            readback = self.broker.status(
                lease_id=group["lease_id"], principal="executor", run_id=f"run-{i}"
            )
            (grant,) = readback["grants"]
            self.assertEqual(grant["issue_id"], f"CHE-{i}")
            self.assertEqual(grant["run_id"], f"run-{i}")
            self.assertEqual(grant["device_id"], group["devices"][0]["device_id"])
            self.assertEqual(grant["reference"], self.host["pool"]["reference"])
        self.assertNotEqual(groups[0]["devices"], groups[1]["devices"])

    def test_missing_adb_never_starts_it_or_renews_physical_expiry(self):
        self.prepare()
        self.assertEqual(self.refresh(), 0)
        path = self.root / "admitted.json"
        prior = path.read_bytes()
        row = json.loads(prior)["devices"][0]
        self.assertEqual(row["endpoint"], "rig-1")
        self.assertEqual(row["grant"]["expires_at"], self.host["expires_at"])
        with (
            patch("refresh._environment", return_value=self.environment),
            patch("subprocess.Popen") as spawn,
        ):
            self.assertEqual(self.refresh(missing=True), 2)
        spawn.assert_not_called()
        self.assertEqual(path.read_bytes(), prior)
        self.assertFalse(self.broker.inventory()["admission_available"])

    def test_ordinary_owner_readiness_cannot_publish_forged_avd_row(self):
        self.prepare()
        registry = self.write_json(
            "avd-registry.json",
            self.registry_rows
            + json.loads(
                (fixtures.ROOT / "tests/fixtures/device_lease/avd.json").read_text(),
            ),
        )
        prior = (self.root / "admitted.json").read_bytes()
        payload = json.loads(prior)
        payload["devices"].append(
            dict(payload["devices"][0], device_id="fixture_api35", endpoint="emulator-5584")
        )
        source = self.write_json("forged.json", payload)
        with self.assertRaisesRegex(ValueError, "requires lifecycle evidence"):
            compose.write_readiness(self.composed, source, fixtures.DeviceRegistry.load(registry))
        mislabeled = fixtures.DeviceRegistry(
            [
                *fixtures.registry().devices(),
                fixtures.Device(
                    "fixture_api35", ("emulator-5584",), restricted=False, identity_confirmed=True
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "requires lifecycle evidence"):
            compose.write_readiness(self.composed, source, mislabeled)
        self.assertEqual((self.root / "admitted.json").read_bytes(), prior)

    def test_reload_preview_reuses_census_validation_without_publishing(self):
        self.prepare()
        rows = self.registry_rows + json.loads(
            (fixtures.ROOT / "tests/fixtures/device_lease/avd.json").read_text()
        )
        registry = self.write_json("candidate.json", rows)
        packet = dict(
            generation=2,
            observed_at=time.time(),
            complete=True,
            writers=["android", "artemis"],
            claims=self.ledger.snapshot()["claims"],
        )
        previous = self.ledger.db_path.read_bytes()

        def preview(**updates):
            census = self.write_json("census.json", packet | updates)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return daemon.main(
                    [
                        "census-android-claims",
                        "--validate-reload",
                        "--composition",
                        str(self.config_file()),
                        "--registry",
                        str(registry),
                        "--state-dir",
                        str(self.root),
                        "--input",
                        str(census),
                    ]
                )

        self.assertEqual(preview(), 0)
        for rejected in (
            {"generation": 1},
            {"observed_at": 0},
            {"claims": []},
            {"complete": False},
            {"writers": ["android"]},
        ):
            with self.subTest(rejected=rejected):
                self.assertEqual(preview(**rejected), 2)
        self.assertEqual(self.ledger.db_path.read_bytes(), previous)
        self.assertEqual(self.ledger.snapshot()["claims"], packet["claims"])

    def test_missing_writer_guard_invalidates_census_without_discarding_claim(self):
        self.prepare()
        self.environment.pop("ARTEMIS_DEVICE_CLAIMS_DB")
        self.assertEqual(self.refresh(), 2)
        self.assertEqual(self.ledger.snapshot()["source"]["complete"], 0)
        self.assertEqual(self.ledger.snapshot()["claims"][0]["claim_id"], self.claim)
        self.assertFalse(self.broker.inventory()["admission_available"])

    def test_expired_or_restricted_grant_does_not_admit(self):
        self.prepare()
        self.host["expires_at"] = time.time() - 1
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(json.loads((self.root / "admitted.json").read_text())["devices"], [])
        self.host["devices"].append("spare-1")
        self.assertEqual(self.refresh(), 2)
        self.assertFalse(self.broker.inventory()["admission_available"])

    def test_changed_staging_wrapper_closes_admission(self):
        self.prepare()
        wrapper = self.root / "adb-disabled"
        wrapper.write_text("exit 1\n")
        wrapper.chmod(0o700)
        self.host["isolated_writers"] = [
            {"unit": "staging.service", "adb_path": str(wrapper), "sha256": "0" * 64}
        ]
        self.environment.update(
            {"ARTEMIS_ADB_PATH": str(wrapper), "ADBUTILS_ADB_PATH": str(wrapper)}
        )
        self.assertEqual(self.refresh(), 2)
        self.assertFalse(self.broker.inventory()["admission_available"])

    def test_refresh_requires_initial_owner_census(self):
        self.prepare()
        with self.ledger.guard(), __import__("sqlite3").connect(self.ledger.db_path) as conn:
            conn.execute("DELETE FROM android_claim_source")
        self.assertEqual(self.refresh(), 2)
        self.assertIsNone(self.ledger.snapshot()["source"])
        self.assertEqual(self.ledger.snapshot()["claims"][0]["claim_id"], self.claim)


if __name__ == "__main__":
    unittest.main()
