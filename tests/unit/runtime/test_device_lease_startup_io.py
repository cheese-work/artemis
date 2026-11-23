"""Linux launch adapter with synthetic files/processes and a fake smart socket."""

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_device_lease_broker  # noqa: F401 -- installs broker source path
from registry import DeviceRegistry
import startup
import compose


class StartupIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root / "fixture.avd"
        self.data.mkdir()
        (self.data / "userdata-qemu.img").write_bytes(b"preserve me")
        self.binary = root / "emulator/emulator"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"fake executable")
        image = root / "system-images/test/system.img"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"fake image")
        config = self.data / "config.ini"
        config.write_text("image.sysdir.1=system-images/test/\n")
        definition = root / "fixture.ini"
        definition.write_text(f"path={self.data}\n")
        device = next(
            d
            for d in DeviceRegistry.load(
                Path(__file__).resolve().parents[2] / "fixtures/device_lease/avd.json"
            ).devices()
            if d.kind == "avd"
        )

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        self.device = replace(
            device,
            avd_data_path=str(self.data),
            avd_definition_path=str(definition),
            emulator_path=str(self.binary),
            emulator_sha256=digest(self.binary),
            config_sha256=digest(config),
            image_sha256=digest(image),
        )
        self.host = dict(mode="local", pid=7000, process_start="1", boot_id="boot")
        self.worker = dict(mode="local", pid=8001, process_start="2", boot_id="boot")
        self.child = dict(mode="local", pid=8002, process_start="3", boot_id="boot")

    def adapter(self):
        return startup.LinuxStartupIO(lambda: self.host)

    def test_preflight_requires_shared_listener_pins_and_no_existing_resource_user(self):
        adapter = self.adapter()
        with (
            patch.object(adapter, "socket_owners", return_value={5037: {7000}}),
            patch.object(adapter, "resource_users", return_value=set()),
            patch("startup.process_identity", return_value=self.host),
        ):
            adapter.preflight(self.device)
            (self.data / "config.ini").write_text("changed")
            with self.assertRaises(ValueError):
                adapter.preflight(self.device)
        self.assertEqual((self.data / "userdata-qemu.img").read_bytes(), b"preserve me")

    def test_prepare_uses_fixed_ports_userdata_and_no_snapshots_and_no_execution(self):
        adapter = self.adapter()
        journal = dict(worker=self.worker, child=self.child)
        process = Mock(pid=8001, stdout=io.StringIO(json.dumps(journal) + "\n"), stdin=Mock())
        with (
            patch("startup.subprocess.Popen", return_value=process) as spawn,
            patch("startup.select.select", return_value=([process.stdout], [], [])),
            patch("startup.process_identity", side_effect=[self.worker, self.child]),
        ):
            self.assertEqual(adapter.prepare(self.device), journal)
        args = spawn.call_args.args[0]
        self.assertIn("-no-snapshot", args)
        self.assertIn("-no-snapshot-load", args)
        self.assertIn("5584,5585", args)
        self.assertEqual(args[args.index("-data") + 1], str(self.data / "userdata-qemu.img"))
        self.assertNotIn("-wipe-data", args)
        process.stdin.write.assert_not_called()
        with patch("startup.process_identity", side_effect=[self.worker, self.child]):
            adapter.resume(journal)
        process.stdin.write.assert_called_once_with("G")

    def test_process_mismatch_or_missing_real_device_prevents_all_guest_probes(self):
        adapter = self.adapter()
        journal = dict(worker=self.worker, child=self.child)
        with (
            patch.object(adapter, "process_evidence", side_effect=ValueError("replacement")),
            patch("startup.devices") as enumeration,
            patch("startup.guest_identity") as guest,
        ):
            with self.assertRaises(ValueError):
                adapter.observe(self.device, journal)
            enumeration.assert_not_called()
            guest.assert_not_called()
        with (
            patch.object(adapter, "process_evidence", return_value={}),
            patch("startup.devices", return_value={"emulator-5590": "device"}),
            patch("startup.guest_identity") as guest,
        ):
            with self.assertRaises(ValueError):
                adapter.observe(self.device, journal)
            guest.assert_not_called()

    def test_process_evidence_accepts_owned_worker_and_exact_child_but_no_competitor(self):
        adapter = self.adapter()
        journal = dict(worker=self.worker, child=self.child)
        args = [
            "-avd",
            self.device.avd_name,
            "-datadir",
            self.device.avd_data_path,
            "-data",
            self.device.avd_data_path + "/userdata-qemu.img",
            "-ports",
            "5584,5585",
        ]
        with (
            patch.object(adapter, "listener"),
            patch.object(adapter, "pins"),
            patch(
                "startup.process_identity",
                side_effect=lambda pid: self.worker if pid == 8001 else self.child,
            ),
            patch("startup.Path.read_text", return_value="8002 (fake qemu) S 8001 0"),
            patch("startup.Path.read_bytes", return_value="\0".join(args).encode()),
            patch.object(
                adapter, "socket_owners", return_value={5584: {8002}, 5585: {8002}}
            ) as sockets,
            patch.object(
                adapter,
                "resource_users",
                side_effect=lambda d, userdata_only=False: (
                    {8002} if userdata_only else {8001, 8002}
                ),
            ),
        ):
            self.assertEqual(adapter.process_evidence(self.device, journal)["child"], self.child)
            sockets.return_value = {5584: {8002}}
            with self.assertRaises(startup.StartupNotReady):
                adapter.process_evidence(self.device, journal)
            sockets.return_value = {5584: {8002, -1}, 5585: {8002}}
            with self.assertRaises(ValueError) as uncertain:
                adapter.process_evidence(self.device, journal)
            self.assertNotIsInstance(uncertain.exception, startup.StartupMismatch)
            sockets.return_value = {5584: {8002, 9000}, 5585: {8002}}
            with self.assertRaises(startup.StartupMismatch):
                adapter.process_evidence(self.device, journal)

    def test_unattributed_competing_socket_is_never_hidden_by_owned_socket(self):
        root = Path(self.temp.name) / "proc"
        (root / "net").mkdir(parents=True)
        rows = (
            "header\n0: 0100007F:13AD 0:0 0A 0 0 0 0 0 101\n1: 00000000:13AD 0:0 0A 0 0 0 0 0 102\n"
        )
        (root / "net/tcp").write_text(rows)
        (root / "net/tcp6").write_text("header\n")
        fd = root / "7000/fd"
        fd.mkdir(parents=True)
        (fd / "3").symlink_to("socket:[101]")
        with patch(
            "startup.Path",
            side_effect=lambda path: (
                root if str(path) == "/proc" else root / str(path).removeprefix("/proc/")
            ),
        ):
            self.assertEqual(self.adapter().socket_owners()[5037], {7000, -1})

    def test_time_wait_rows_do_not_compete_with_live_or_free_fixed_ports(self):
        root = Path(self.temp.name) / "proc"
        (root / "net").mkdir(parents=True)
        # Linux /proc/net/tcp shape: active-close TIME_WAIT rows have inode 0.
        (root / "net/tcp").write_text(
            "header\n"
            "0: 0100007F:13AD 00000000:0000 0A 0 0 0 1000 0 101\n"
            "1: 0100007F:13AD 0100007F:C001 06 0 0 0 0 0 0\n"
            "2: 0100007F:15D0 0100007F:C002 06 0 0 0 0 0 0\n"
            "3: 0100007F:15D1 0100007F:C003 06 0 0 0 0 0 0\n"
        )
        (root / "net/tcp6").write_text(
            "header\n0: 00000000000000000000000001000000:13AD "
            "00000000000000000000000001000000:C004 06 0 0 0 0 0 0\n"
        )
        fd = root / "7000/fd"
        fd.mkdir(parents=True)
        (fd / "3").symlink_to("socket:[101]")
        with patch(
            "startup.Path",
            side_effect=lambda path: (
                root if str(path) == "/proc" else root / str(path).removeprefix("/proc/")
            ),
        ):
            self.assertEqual(self.adapter().socket_owners(), {5037: {7000}})

    def test_real_loopback_active_close_keeps_only_the_live_listener_owner(self):
        # No ADB/emulator traffic: an ephemeral fake server supplies a real
        # kernel TIME_WAIT row. Inspect only this test process's descriptors.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            with socket.create_connection(server.getsockname()) as client:
                accepted, _ = server.accept()
                with accepted:
                    accepted.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
            rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
            self.assertTrue(
                any(
                    int(row.split()[1].rsplit(":", 1)[1], 16) == port
                    and row.split()[3] == "06"
                    and row.split()[9] == "0"
                    for row in rows
                )
            )
            with patch("startup.Path.glob", return_value=[Path("/proc/self").resolve()]):
                self.assertEqual(self.adapter().socket_owners()[port], {os.getpid()})

    def test_unreadable_process_descriptors_explicitly_refuse_complete_visibility(self):
        directory = Mock()
        descriptors = Mock()
        descriptors.iterdir.side_effect = PermissionError("other uid process")
        directory.__truediv__ = Mock(return_value=descriptors)
        adapter = self.adapter()
        with (
            patch("startup.Path.glob", return_value=[directory]),
            patch("startup.Path.read_text", return_value="header\n"),
        ):
            for inspect in (adapter.socket_owners, lambda: adapter.resource_users(self.device)):
                with self.subTest(inspect=inspect):
                    with self.assertRaisesRegex(ValueError, "full host /proc visibility"):
                        inspect()

    def test_identity_wire_names_only_exact_serial_and_fixed_read_only_properties(self):
        class Wire:
            def __init__(self, value):
                self.response = io.BytesIO(b"OKAYOKAY" + value)
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def sendall(self, value):
                self.sent.append(value)

            def recv(self, count):
                return self.response.read(count)

        wires = [Wire(value) for value in (b"fixture_api35\n", b"35\n", b"x86_64\n", b"1\n")]
        with patch("startup.socket.create_connection", side_effect=wires) as connect:
            identity = startup.guest_identity("emulator-5584")
        self.assertEqual(
            identity, dict(avd_name="fixture_api35", api=35, abi="x86_64", boot_complete="1")
        )
        for call, wire in zip(connect.call_args_list, wires):
            self.assertEqual(call.args[0], ("127.0.0.1", 5037))
            self.assertEqual(wire.sent[0], b"001chost:transport:emulator-5584")
            self.assertIn(b"shell:getprop ", wire.sent[1])

    def test_owner_host_preflight_is_fresh_bounded_and_not_a_bare_listener_claim(self):
        path = Path(self.temp.name) / "shared-adb.json"
        row = dict(
            observed_at=1000,
            host="127.0.0.1",
            port=5037,
            listener=self.host,
            evidence="ops-scoped-host-preflight",
        )
        path.write_text(json.dumps(row))
        path.chmod(0o600)
        reader = compose.shared_adb_reader(path, 60, clock=lambda: 1001)
        self.assertEqual(reader(), self.host)
        for changes in (
            dict(observed_at=900),
            dict(port=5038),
            dict(evidence=""),
            dict(listener=dict(self.host, pid=True)),
        ):
            path.write_text(json.dumps(dict(row, **changes)))
            with self.assertRaises(ValueError):
                reader()

    def test_real_paused_worker_with_fake_executable_cannot_run_before_gate(self):
        marker = Path(self.temp.name) / "executed"
        self.binary.write_text(
            f"#!{sys.executable}\nimport os\nfrom pathlib import Path\n"
            "assert os.read(0, 1) == b''\n"
            "assert os.readlink('/proc/self/fd/0') == '/dev/null'\n"
            "assert os.readlink('/proc/self/fd/1') == '/dev/null'\n"
            f"Path({str(marker)!r}).write_text('fake only')\n"
        )
        self.binary.chmod(0o700)
        adapter = self.adapter()
        journal = adapter.prepare(self.device)
        self.assertFalse(marker.exists())
        self.assertNotEqual(journal["worker"]["pid"], journal["child"]["pid"])
        adapter.abort(journal)
        self.assertFalse(marker.exists())
        journal = adapter.prepare(self.device)
        self.assertFalse(marker.exists())
        adapter.resume(journal)
        adapter._running[journal["worker"]["pid"]].wait(timeout=5)
        self.assertEqual(marker.read_text(), "fake only")

    def test_worker_allows_bounded_slow_journal_recheck_before_forwarding_gate(self):
        with (
            patch("startup.os.pipe", return_value=(10, 11)),
            patch("startup.os.fork", return_value=8002),
            patch("startup.os.close"),
            patch("startup.os.waitpid"),
            patch("startup.process_identity", return_value=self.worker),
            patch("builtins.print"),
            patch(
                "startup.select.select",
                side_effect=lambda r, w, x, timeout: (r if timeout >= 120 else [], [], []),
            ),
            patch("startup.os.read", return_value=b"G"),
            patch("startup.os.write") as forward,
        ):
            startup._worker([str(self.binary)])
        forward.assert_called_once_with(11, b"G")


if __name__ == "__main__":
    unittest.main()
