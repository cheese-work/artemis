"""DLP-1 registry contract; synthetic aliases only, no ADB or Multica calls."""

from dataclasses import FrozenInstanceError, replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


REGISTRY_PATH = Path(__file__).resolve().parents[3] / "tools/device-lease/registry.py"
SPEC = importlib.util.spec_from_file_location("dlp_device_registry", REGISTRY_PATH)
registry_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry_module
SPEC.loader.exec_module(registry_module)
Device = registry_module.Device
DeviceRegistry = registry_module.DeviceRegistry


def phone(**changes):
    return replace(
        Device(
            device_id="rig-phone-1",
            aliases=("usb-fixture-1", "192.0.2.10:5555"),
            pool="rig",
            model="Fixture phone",
            api=34,
            abi="arm64-v8a",
            restricted=False,
            identity_confirmed=True,
        ),
        **changes,
    )


class DeviceRegistryTests(unittest.TestCase):
    def test_usb_and_wireless_resolve_one_canonical_device_with_metadata(self):
        device = phone()
        registry = DeviceRegistry([device])
        for alias in (device.device_id, *device.aliases):
            with self.subTest(alias=alias):
                self.assertIs(registry.resolve(alias), device)
        self.assertEqual(
            (device.pool, device.model, device.api, device.abi),
            ("rig", "Fixture phone", 34, "arm64-v8a"),
        )
        self.assertIsNone(registry.resolve("unregistered"))

    def test_only_registered_confirmed_device_state_is_eligible(self):
        registry = DeviceRegistry([phone()])
        self.assertTrue(registry.eligible("usb-fixture-1", "device"))
        for state in ("unauthorized", "no permissions", "offline", "unknown", "", None):
            with self.subTest(state=state):
                self.assertFalse(registry.eligible("usb-fixture-1", state))
        self.assertFalse(registry.eligible("unregistered", "device"))
        unresolved = DeviceRegistry([phone(identity_confirmed=False)])
        self.assertFalse(unresolved.eligible("192.0.2.10:5555", "device"))

    def test_500c592_is_registered_restricted_and_never_raw_adb_eligible(self):
        registry = DeviceRegistry()
        device = registry.resolve("500c592")
        self.assertIsNotNone(device)
        self.assertTrue(device.restricted)
        self.assertEqual(device.pool, "root")
        self.assertFalse(registry.eligible("500c592", "device"))
        # A renamed canonical id or a false flag cannot remove this serial's restriction.
        device = phone(aliases=("500c592", "192.0.2.11:5555"), restricted_policy="che-2129")
        self.assertTrue(device.restricted)
        registry = DeviceRegistry([device])
        for alias in (device.device_id, *device.aliases):
            self.assertFalse(registry.eligible(alias, "device"))

    def test_restricted_unknown_policy_and_unresolved_identity_fail_closed(self):
        for policy in (None, "", "unknown", "raw_adb", "CHE-2129"):
            with self.subTest(policy=policy):
                registry = DeviceRegistry([phone(restricted=True, restricted_policy=policy)])
                self.assertFalse(registry.eligible("usb-fixture-1", "device"))
                self.assertFalse(registry.eligible("usb-fixture-1", "device", route="che-2129"))
        registry = DeviceRegistry([phone(restricted=True, restricted_policy="che-2129")])
        self.assertFalse(registry.eligible("usb-fixture-1", "device"))
        self.assertTrue(registry.eligible("usb-fixture-1", "device", route="che-2129"))
        self.assertFalse(registry.eligible("usb-fixture-1", "unauthorized", route="che-2129"))
        registry = DeviceRegistry(
            [phone(restricted=True, restricted_policy="che-2129", identity_confirmed=False)]
        )
        self.assertFalse(registry.eligible("usb-fixture-1", "device", route="che-2129"))
        self.assertFalse(
            DeviceRegistry([phone()]).eligible("usb-fixture-1", "device", route="typo")
        )

    def test_ambiguous_aliases_and_canonical_id_collisions_are_rejected(self):
        for second in (
            phone(),
            phone(device_id="second"),
            phone(device_id="usb-fixture-1", aliases=("other",)),
        ):
            with self.subTest(second=second):
                with self.assertRaises(ValueError):
                    DeviceRegistry([phone(), second])

    def test_invalid_metadata_is_rejected_and_records_are_immutable(self):
        for changes in (
            {"device_id": ""},
            {"aliases": ("",)},
            {"aliases": "usb"},
            {"restricted": "false"},
            {"identity_confirmed": "true"},
            {"api": True},
            {"api": 0},
            {"pool": ""},
            {"abi": 123},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    phone(**changes)
        with self.assertRaises(FrozenInstanceError):
            phone().restricted = True

    def test_load_registry_json_and_reject_malformed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "device_id": "registered",
                            "aliases": ["usb", "192.0.2.12:5555"],
                            "pool": "rig",
                            "model": None,
                            "api": None,
                            "abi": None,
                            "restricted": False,
                            "identity_confirmed": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            registry = DeviceRegistry.load(path)
            self.assertEqual(registry.resolve("usb").device_id, "registered")
            self.assertTrue(registry.eligible("192.0.2.12:5555", "device"))
            self.assertTrue(registry.resolve("500c592").restricted)
            for content in (
                "{}",
                '[{"device_id": "x", "pool": "rig", "restricted": "false"}]',
                '[{"device_id": "x", "pool": "rig", "extra": true}]',
                "{broken",
            ):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    DeviceRegistry.load(path)


if __name__ == "__main__":
    unittest.main()
