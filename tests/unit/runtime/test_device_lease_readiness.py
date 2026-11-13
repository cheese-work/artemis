"""Live grant admission: no device I/O; owner records are fixtures."""
import json
import math
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_compose as fixtures
from test_device_lease_compose import registry
import compose


class ReadinessTests(unittest.TestCase):
    setUp = fixtures.ComposeTests.setUp
    write_json = fixtures.ComposeTests.write_json
    write_admitted = fixtures.ComposeTests.write_admitted
    def records(self):
        now = time.time()
        return {"generation": 1, "devices": [{
            "device_id": "rig-1", "endpoint": "usb-rig-1", "state": "device",
            "observed_at": now, "grant": {"reference": "CHE-fixture-grant",
            "observed_at": now, "expires_at": now + 300}}]}

    def test_registry_map_is_not_a_grant(self):
        self.write_json("admitted.json", {"observed_at": time.time(), "devices": {"rig-1": "usb-rig-1"}})
        with self.assertRaises(ValueError):
            compose.admitted_reader(self.root / "admitted.json", 60)()

    def test_per_device_grants_and_observations(self):
        payload = self.records()
        path = self.write_json("ready.json", payload)
        # The live reader resolves aliases using trusted inventory.
        read = lambda: compose.admitted_reader(path, 60, registry())()
        self.assertEqual(read(), {"rig-1": "usb-rig-1"})
        row = payload["devices"][0]
        for field, value in [("expires_at", time.time() - 1), ("observed_at", 0)]:
            original = row["grant"][field]
            row["grant"][field] = value
            self.write_json("ready.json", payload)
            self.assertEqual(read(), {})
            row["grant"][field] = original
        row["observed_at"] = 0
        self.write_json("ready.json", payload)
        self.assertEqual(read(), {})

    def test_unknown_endpoint_restricted_spare_and_invalid_times_refuse(self):
        from broker import Device, DeviceRegistry
        reg = DeviceRegistry([*registry().devices(), Device("unused-spare", pool="spare",
                              restricted=False, identity_confirmed=True)])
        for identity, endpoint in [("rig-1", "other"), ("500c592", "500c592"),
                                    ("unused-spare", "unused-spare")]:
            payload = self.records()
            payload["devices"][0].update(device_id=identity, endpoint=endpoint)
            path = self.write_json("ready.json", payload)
            self.assertEqual(compose.admitted_reader(path, 60, reg)(), {})
        for bad in [math.nan, math.inf, True, "now"]:
            payload = self.records()
            payload["devices"][0]["observed_at"] = bad
            path = self.write_json("ready.json", payload)
            with self.assertRaises(ValueError):
                compose.admitted_reader(path, 60, registry())()


if __name__ == "__main__":
    unittest.main()
