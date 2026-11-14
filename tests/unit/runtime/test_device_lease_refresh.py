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

    def refresh(self):
        config = self.write_json("host.json", self.host)
        registry = self.write_json(
            "registry.json",
            [
                {
                    "device_id": "rig-1",
                    "aliases": ["usb-rig-1"],
                    "restricted": False,
                    "identity_confirmed": True,
                },
                {"device_id": "rig-2", "restricted": False, "identity_confirmed": True},
                {"device_id": "spare-1", "pool": "spare"},
            ],
        )
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
                return_value=Wire(b"rig-1\tdevice product:fixture\nspare-1\tdevice\n"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            try:
                return daemon.main(
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
                return exc.code

    def test_cli_refresh_observes_grants_and_retains_existing_claim(self):
        self.prepare()
        self.assertEqual(self.refresh(), 0, "owner refresh must succeed for a guarded live writer")
        admitted = json.loads((self.root / "admitted.json").read_text())
        self.assertEqual([r["device_id"] for r in admitted["devices"]], ["rig-1"])
        self.assertEqual(self.ledger.snapshot()["claims"][0]["claim_id"], self.claim)
        self.assertTrue(self.broker.inventory()["admission_available"])

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
