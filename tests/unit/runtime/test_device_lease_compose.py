"""Production `serve` composition (CHE-5297 slice A); fake devices, recovery and claims."""

import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import Broker, Device, DeviceRegistry, Refused
import compose
from compose import CompositionError
import daemon

LEADER = "5cd75ce5-d323-4004-9779-7e2ccf16bde4"


class FakeRecovery:
    def fence(self, group, *, timeout):
        return True

    def undo(self, group, change, *, timeout):
        return True


class NoFence:
    def undo(self, group, change, *, timeout):
        return True


def registry():
    return DeviceRegistry(
        [
            Device("rig-1", ("usb-rig-1",), restricted=False, identity_confirmed=True),
            Device("rig-2", restricted=False, identity_confirmed=True),
            Device("spare-1", pool="spare"),
        ]
    )


class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        (self.root / "locks").mkdir()
        self.write_admitted({"rig-1": "usb-rig-1", "rig-2": "rig-2"})
        self.write_json("android.json", {"observed_at": time.time(), "claims": []})
        self.config = {
            "admitted": {"path": str(self.root / "admitted.json"), "max_age_seconds": 60},
            "artemis_locks_dir": str(self.root / "locks"),
            "android_claims": {"path": str(self.root / "android.json"), "max_age_seconds": 60},
            "guard_path": str(self.root / "claims.guard"),
            "recovery": {
                "adapter": "test_device_lease_compose:FakeRecovery",
                "owner": "ops-fixture",
                "ops_issue": "CHE-fixture-ops",
            },
            "leader_control_socket": str(self.root / "leader" / "control.sock"),
        }

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload))
        os.chmod(path, 0o600)
        return path

    def write_admitted(self, devices, age=0):
        self.write_json("admitted.json", {"observed_at": time.time() - age, "devices": devices})

    def config_file(self, config=None):
        return self.write_json("composition.json", self.config if config is None else config)

    def composed_broker(self):
        composed = compose.compose(self.config_file(), registry())
        return Broker(self.root / "leases.sqlite3", registry(), **composed["broker"]), composed

    def take(self, broker, **changes):
        request = dict(
            principal="agent",
            run_id="run-1",
            purpose="CHE-fixture",
            ttl=300,
            idempotency_key="k",
        )
        request.update(changes)
        return broker.acquire(**request)

    def test_every_missing_composition_input_refuses(self):
        paths = [
            ("admitted",),
            ("admitted", "max_age_seconds"),
            ("artemis_locks_dir",),
            ("android_claims",),
            ("android_claims", "path"),
            ("guard_path",),
            ("recovery",),
            ("recovery", "adapter"),
            ("recovery", "owner"),
            ("recovery", "ops_issue"),
            ("leader_control_socket",),
        ]
        for path in paths:
            with self.subTest(missing=".".join(path)):
                config = json.loads(json.dumps(self.config))
                parent = config
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaises(CompositionError) as caught:
                    compose.compose(self.config_file(config), registry())
                self.assertIn(path[0], str(caught.exception))

    def test_recovery_adapter_without_fence_refuses(self):
        self.config["recovery"]["adapter"] = "test_device_lease_compose:NoFence"
        with self.assertRaises(CompositionError):
            compose.compose(self.config_file(), registry())

    def test_group_writable_composition_file_refuses(self):
        path = self.config_file()
        os.chmod(path, 0o620)
        with self.assertRaises(CompositionError):
            compose.compose(path, registry())

    def test_serve_refuses_partial_composition_before_binding(self):
        config = dict(self.config)
        del config["android_claims"]
        state = self.root / "state"
        err = io.StringIO()
        with redirect_stderr(err):
            code = daemon.main(
                ["serve", "--state-dir", str(state), "--composition", str(self.config_file(config))]
            )
        self.assertEqual(code, 2)
        self.assertIn("android_claims", err.getvalue())
        self.assertFalse((state / "broker.sock").exists())

    def test_composed_broker_admits_only_with_fresh_readiness_and_no_claims(self):
        broker, _ = self.composed_broker()
        got = self.take(broker, device_id="rig-1")
        self.assertEqual(got["devices"][0]["endpoint"], "usb-rig-1")
        inventory = {d["device_id"]: d for d in broker.inventory()["devices"]}
        self.assertTrue(inventory["rig-2"]["eligible"])
        self.assertEqual(inventory["spare-1"]["reason"], "restricted")

        self.write_admitted({"rig-1": "usb-rig-1", "rig-2": "rig-2"}, age=120)
        refused = self.assertRaises(Refused)
        with refused:
            self.take(broker, run_id="run-2", idempotency_key="k2", device_id="rig-2")
        self.assertEqual(refused.exception.code, "admission_unavailable")
        self.assertFalse(broker.inventory()["admission_available"])

    def test_artemis_lock_and_android_claim_block_their_devices(self):
        broker, _ = self.composed_broker()
        lock = self.root / "locks" / "artemis-device-rig-2.lock"
        lock.write_text(json.dumps({"device_id": "rig-2", "pid": 1}))
        compose.write_claims_snapshot(
            self.root / "android.json", compose.FileGuard(self.root / "claims.guard"), ["usb-rig-1"]
        )
        rows = {d["device_id"]: d for d in broker.inventory()["devices"]}
        self.assertTrue(rows["rig-1"]["claimed"])
        self.assertTrue(rows["rig-2"]["claimed"])
        with self.assertRaises(Refused) as caught:
            self.take(broker, device_id="rig-1")
        self.assertEqual(caught.exception.code, "device_busy")

    def test_unreadable_artemis_lock_closes_admission(self):
        broker, _ = self.composed_broker()
        (self.root / "locks" / "artemis-device-rig-2.lock").write_text("{partial")
        self.assertFalse(broker.inventory()["admission_available"])

    def test_guard_is_held_while_claim_sources_are_read(self):
        broker, _ = self.composed_broker()
        observed = []

        def probe():
            fd = os.open(self.root / "claims.guard", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append("free")
            except BlockingIOError:
                observed.append("held")
            finally:
                os.close(fd)
            return []

        broker.admission_sources["android"] = probe
        broker.inventory()
        self.assertEqual(observed, ["held"])

    def test_leader_control_socket_verifies_release_and_holder_socket_cannot(self):
        broker, composed = self.composed_broker()
        group = self.take(broker, device_id="rig-1")
        broker.release(lease_id=group["lease_id"], principal="agent", run_id="run-1")
        broker.maintain()
        state = self.root / "state"
        holder = daemon.BrokerServer(state, broker)
        control = daemon.ControlServer(composed["leader_control_socket"], broker)
        worker = threading.Thread(target=control.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            with self.assertRaises(Refused):
                holder.dispatch({"op": "verify-release", "args": {"lease_id": group["lease_id"]}})
            self.assertEqual((self.root / "leader").stat().st_mode & 0o777, 0o700)
            done = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/device-lease/device-lease"),
                    "--control-socket",
                    str(composed["leader_control_socket"]),
                    "verify-release",
                    "--lease-id",
                    group["lease_id"],
                    "--verifier",
                    LEADER,
                    "--evidence",
                    "CHE-fixture#evidence",
                    "--passed",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            control.shutdown()
            worker.join(timeout=5)
            control.server_close()
            holder.server_close()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "released")


if __name__ == "__main__":
    unittest.main()
