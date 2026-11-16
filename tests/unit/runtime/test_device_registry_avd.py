"""AVD metadata is inventory only; all broker state and endpoints are synthetic."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
from broker import Broker, DeviceRegistry, Refused

FIXTURE = ROOT / "tests/fixtures/device_lease/avd.json"


class AvdRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.row = json.loads(FIXTURE.read_text())[0]
        self.path = Path(self.temp.name) / "registry.json"

    def load(self, *rows):
        self.path.write_text(json.dumps(list(rows)))
        return DeviceRegistry.load(self.path)

    def valid_registry(self, *rows):
        try:
            return self.load(*(rows or (self.row,)))
        except ValueError as exc:
            self.fail(f"valid powered-off AVD metadata must be accepted: {exc}")

    def test_powered_off_metadata_round_trips_and_aliases_share_one_slot(self):
        registry = self.valid_registry()
        device = registry.resolve(self.row["device_id"])
        self.assertEqual(device.kind, "avd")
        for name, value in self.row.items():
            self.assertEqual(getattr(device, name), tuple(value) if name == "aliases" else value)
        self.assertEqual(
            [d.device_id for d in registry.devices() if d.pool == "avd"], [device.device_id]
        )
        for alias in device.aliases:
            self.assertIs(registry.resolve(alias), device)
        restored = self.valid_registry(asdict(device)).resolve(device.device_id)
        self.assertEqual(restored, device)

    def test_missing_and_malformed_lifecycle_inputs_fail_closed(self):
        required = (
            "avd_name",
            "avd_data_path",
            "avd_definition_path",
            "console_port",
            "adb_port",
            "emulator_path",
            "emulator_sha256",
            "config_sha256",
            "image_sha256",
            "api",
            "abi",
        )
        for field in required:
            for absent in (True, False):
                row = dict(self.row)
                if absent:
                    del row[field]
                else:
                    row[field] = None
                with self.subTest(field=field, absent=absent), self.assertRaises(ValueError):
                    self.load(row)
        for changes in (
            {"kind": "unknown"},
            {"kind": None},
            {"pool": "rig"},
            {"avd_name": "../other"},
            {"avd_name": " name"},
            {"avd_name": ""},
            {"console_port": True},
            {"console_port": "5584"},
            {"console_port": 5585},
            {"console_port": 5552, "adb_port": 5553},
            {"console_port": 5684, "adb_port": 5685},
            {"adb_port": 5586},
            {"adb_port": True},
            {"adb_port": "5585"},
            {"avd_data_path": "relative.avd"},
            {"avd_data_path": "/fixture/../other.avd"},
            {"avd_data_path": "/fixture/avd/"},
            {"avd_data_path": "/"},
            {"avd_data_path": "/fixture/\u0000.avd"},
            {"avd_data_path": 12},
            {"avd_definition_path": "relative.ini"},
            {"emulator_path": "~/emulator"},
            {"emulator_sha256": "latest"},
            {"config_sha256": "a" * 63},
            {"image_sha256": "g" * 64},
            {"image_sha256": "A" * 64},
            {"abi": ""},
            {"abi": " x86_64"},
            {"api": True},
            {"aliases": []},
            {"aliases": ["emulator-5586"]},
            {"aliases": ["emulator-5584", "127.0.0.1:5587"]},
            {"aliases": ["emulator-5584", "emulator-5584"]},
            {"device_id": "emulator-5584"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load(self.row | changes)

    def test_noncanonical_symlink_path_is_rejected(self):
        real = Path(self.temp.name) / "real.avd"
        real.mkdir()
        link = Path(self.temp.name) / "alias.avd"
        link.symlink_to(real, target_is_directory=True)
        for field in ("avd_data_path", "avd_definition_path", "emulator_path"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(self.row | {field: str(link)})
        self.valid_registry(self.row | {"avd_data_path": str(real.resolve())})

    def test_duplicates_alias_conflicts_and_second_avd_cannot_add_capacity(self):
        self.valid_registry()
        for second in (
            self.row,
            self.row | {"device_id": "renamed"},
            self.row
            | {
                "device_id": "other",
                "avd_name": "other",
                "console_port": 5586,
                "adb_port": 5587,
                "aliases": ["emulator-5586"],
                "avd_data_path": "/fixture/other.avd",
            },
            {"device_id": "physical", "aliases": ["emulator-5584"]},
            {"device_id": "127.0.0.1:5585"},
        ):
            with self.subTest(second=second), self.assertRaises(ValueError):
                self.load(self.row, second)

    def test_physical_records_and_restrictions_are_unchanged(self):
        # Ten synthetic physical records; never read or rewrite the live fleet.
        physical = (
            [
                {"device_id": f"rig-{n}", "restricted": False, "identity_confirmed": True}
                for n in range(6)
            ]
            + [{"device_id": f"spare-{n}", "pool": "spare"} for n in range(3)]
            + [{"device_id": "500c592", "pool": "root", "restricted": False}]
        )
        before = self.load(*physical)
        after = self.valid_registry(*physical, self.row)
        for old in before.devices():
            self.assertEqual(after.resolve(old.device_id), old)
            for state in ("device", "offline", None):
                self.assertEqual(
                    after.eligible(old.device_id, state), before.eligible(old.device_id, state)
                )
        self.assertEqual(len(after.devices()), 11)
        self.assertTrue(after.resolve("500c592").restricted)
        for changes in ({"pool": "avd"}, {"avd_name": "hidden-avd"}, {"console_port": 5584}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load(physical[0] | changes)

    def test_metadata_or_device_label_never_authorizes_ordinary_work(self):
        for confirmed in (False, True):
            registry = self.valid_registry(self.row | {"identity_confirmed": confirmed})
            device = registry.resolve(self.row["device_id"])
            for alias in (device.device_id, *device.aliases):
                for state in (None, "offline", "starting", "unconfirmed", "device"):
                    self.assertFalse(registry.eligible(alias, state))
            # Even an injected ordinary readiness map cannot enable the new route.
            broker = Broker(
                Path(self.temp.name) / f"leases-{confirmed}.db",
                registry,
                admitted={device.device_id: device.aliases[0]},
                clock=lambda: 100_000.0,
                monotonic=lambda: 100_000.0,
                admission_sources={"artemis": lambda: [], "android": lambda: []},
                admission_guard=threading.RLock,
            )
            row = next(
                d for d in broker.inventory()["devices"] if d["device_id"] == device.device_id
            )
            self.assertFalse(row["eligible"])
            self.assertIsNone(row["lease"])
            for alias in (None, device.device_id, *device.aliases):
                for count in (1, 2):
                    with self.subTest(confirmed=confirmed, alias=alias, count=count):
                        with self.assertRaises(Refused) as caught:
                            broker.acquire(
                                principal="fixture",
                                run_id="fixture-run",
                                purpose="CHE-fixture",
                                pool="avd",
                                device_id=alias,
                                count=count,
                                ttl=300,
                                idempotency_key=f"{alias}-{count}",
                            )
                        self.assertEqual(caught.exception.code, "impossible_batch")
            with sqlite3.connect(broker.db_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM broker_groups").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
