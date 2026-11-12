"""Owner fleet inventory readback (CHE-5297 slice B); synthetic devices only."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
from broker import Broker, Device, DeviceRegistry, Refused
from daemon import BrokerServer


def fleet():
    return DeviceRegistry(
        [
            Device("rig-1", ("usb-rig-1",), restricted=False, identity_confirmed=True),
            Device("rig-2", restricted=False, identity_confirmed=True),
            Device("rig-new", restricted=False, identity_confirmed=False),
            Device("rig-unready", restricted=False, identity_confirmed=True),
            Device("spare-1", pool="spare"),
        ]
    )


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100_000.0
        guard = threading.RLock()
        self.broker = Broker(
            Path(self.temp.name) / "leases.db",
            fleet(),
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            admitted={"rig-1": "usb-rig-1", "rig-2": "rig-2", "spare-1": "spare-1"},
            admission_sources={"artemis": lambda: ["rig-2"], "android": lambda: []},
            admission_guard=lambda: guard,
        )

    def test_lists_every_registry_device_with_eligibility_reason_and_lease(self):
        lease = self.broker.acquire(
            principal="agent",
            run_id="run-1",
            purpose="CHE-fixture",
            device_id="rig-1",
            ttl=300,
            idempotency_key="k",
        )
        got = self.broker.inventory()
        self.assertTrue(got["admission_available"])
        rows = {d["device_id"]: d for d in got["devices"]}
        self.assertEqual(
            sorted(rows),
            ["500c592", "rig-1", "rig-2", "rig-new", "rig-unready", "spare-1"],
        )
        expected = {
            "rig-1": (True, None),
            "rig-2": (True, None),
            "rig-new": (False, "identity_unconfirmed"),
            "rig-unready": (False, "not_admitted"),
            "spare-1": (False, "restricted"),
            "500c592": (False, "restricted"),
        }
        for device_id, (eligible, reason) in expected.items():
            with self.subTest(device_id=device_id):
                self.assertEqual((rows[device_id]["eligible"], rows[device_id]["reason"]),
                                 (eligible, reason))
        self.assertEqual(rows["spare-1"]["pool"], "spare")
        self.assertEqual(rows["500c592"]["pool"], "root")
        self.assertEqual(rows["rig-1"]["lease"]["lease_id"], lease["lease_id"])
        self.assertEqual(rows["rig-1"]["lease"]["state"], "active")
        self.assertIsNone(rows["rig-2"]["lease"])
        self.assertTrue(rows["rig-2"]["claimed"])
        self.assertFalse(rows["rig-1"]["claimed"])

    def test_closed_admission_is_reported_not_hidden(self):
        self.broker.admission_sources = {"artemis": lambda: []}
        got = self.broker.inventory()
        self.assertFalse(got["admission_available"])
        self.assertEqual(len(got["devices"]), 6)

    def test_cli_inventory_needs_no_lease_identity(self):
        state = Path(self.temp.name) / "state"
        server = BrokerServer(state, self.broker)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            done = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/device-lease/device-lease"),
                    "--socket",
                    str(state / "broker.sock"),
                    "inventory",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
        self.assertEqual(done.returncode, 0, done.stderr)
        rows = {d["device_id"]: d for d in json.loads(done.stdout)["devices"]}
        self.assertEqual(rows["spare-1"]["reason"], "restricted")
        self.assertTrue(rows["rig-1"]["eligible"])

    def test_inventory_rpc_accepts_no_arguments(self):
        server = BrokerServer(Path(self.temp.name) / "state", self.broker)
        try:
            self.assertIn("devices", server.dispatch({"op": "inventory", "args": {}}))
            with self.assertRaises(Refused):
                server.dispatch({"op": "inventory", "args": {"principal": "x"}})
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
