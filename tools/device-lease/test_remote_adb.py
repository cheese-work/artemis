"""Offline R1 wire checks. No device, SSH, Multica or shared ADB access."""

import asyncio
import base64
import dataclasses
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Load the real stdlib-only runtime modules without importing the eager SDK facade.
ROOT = Path(__file__).resolve().parents[2]
for name, directory in (
    ("artemis", ROOT / "artemis"),
    ("artemis.runtime", ROOT / "artemis/runtime"),
):
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(directory)]
        sys.modules[name] = package

from artemis.runtime.adb_gateway import pack_message
from remote_adb import LeaseGateway, RemoteAccess


class Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class Remote:
    def __init__(self):
        self.requests = []
        self.reader = asyncio.StreamReader()

    async def write(self, data):
        self.requests.append(data)
        command = data[4:]
        self.reader.feed_data(b"OKAY")
        if command.startswith(b"host:tport:serial:"):
            self.reader.feed_data(b"12345678")
        elif command.startswith(b"host:transport:"):
            pass
        elif command.endswith(b"features"):
            self.reader.feed_data(pack_message(b"shell_v2,untrusted_feature"))
        elif command.startswith(b"host:track-devices") or command == b"host:devices":
            self.reader.feed_data(pack_message(b"phone\tdevice\nhidden\tdevice\n"))
        elif b"wait-for-" in command:
            self.reader.feed_data(b"OKAY")
        elif command == b"host:version":
            self.reader.feed_data(pack_message(b"0029"))
        else:
            self.reader.feed_data(b"output")

    async def read(self, size):
        return await self.reader.read(size)

    def finish(self):
        self.reader.feed_eof()


async def exchange(payload, gateway=None, remote=None):
    reader, writer, remote = asyncio.StreamReader(), Writer(), remote or Remote()
    reader.feed_data(payload)
    reader.feed_eof()
    await (gateway or LeaseGateway(lambda: {"phone"})).relay(reader, writer, remote)
    return writer, remote


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_raw_stream_uses_backpressure_without_losing_bytes(self):
        from remote_adb import listen_loopback

        payload = bytes(range(256)) * 4096
        received = bytearray()
        outcome = asyncio.get_running_loop().create_future()

        async def echo(reader, writer):
            try:
                while len(received) < len(payload):
                    chunk = await reader.read(8192)
                    if not chunk:
                        break
                    received.extend(chunk)
                    writer.write(chunk)
                    await writer.drain()
                outcome.set_result(True)
            except Exception:
                outcome.set_result(False)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await listen_loopback(echo, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1]
        )
        try:
            writer.write(payload)

            async def consume():
                try:
                    return await reader.readexactly(len(payload))
                except (asyncio.IncompleteReadError, ConnectionError):
                    return b""

            _, returned = await asyncio.wait_for(asyncio.gather(writer.drain(), consume()), 2)
            self.assertTrue(await outcome, "large raw bursts must not abort the bounded reader")
            self.assertEqual(bytes(received), payload)
            self.assertEqual(returned, payload)
        finally:
            writer.close()
            await asyncio.gather(writer.wait_closed(), return_exceptions=True)
            server.close()
            await server.wait_closed()

    async def test_shared_stream_closure_has_a_deadline_and_aborts_stalled_writer(self):
        from artemis.runtime.stream_owner import StreamOwner

        class Stalled(Writer):
            async def wait_closed(self):
                await asyncio.Future()

        writer = Stalled()
        aborted = []
        writer.transport = types.SimpleNamespace(abort=lambda: aborted.append(True))
        owner = StreamOwner()
        owner.clients[writer] = None
        with patch("artemis.runtime.stream_owner.CLOSE_SECONDS", 0.01, create=True):
            task = asyncio.create_task(owner.close_streams())
            try:
                try:
                    result = await asyncio.wait_for(asyncio.shield(task), 0.1)
                except TimeoutError:
                    self.fail("B2/R1 shared close exceeded its deadline")
                self.assertIs(result, False)
                self.assertTrue(writer.closed)
                self.assertEqual(aborted, [True])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_partial_request_and_upstream_status_share_one_completion_deadline(self):
        class PartialRemote(Remote):
            async def write(self, data):
                self.requests.append(data)
                self.reader.feed_data(b"O")

        for upstream in (False, True):
            reader, writer, remote = asyncio.StreamReader(), Writer(), PartialRemote()
            reader.feed_data(pack_message(b"host:version") if upstream else b"0")
            with patch("artemis.runtime.adb_gateway.FRAME_SECONDS", 0.01, create=True):
                task = asyncio.create_task(
                    LeaseGateway(lambda: {"phone"}).relay(reader, writer, remote)
                )
                try:
                    await asyncio.wait_for(asyncio.shield(task), 0.1)
                except TimeoutError:
                    self.fail("partial frame/status exceeded its completion deadline")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertIn(b"FAIL", writer.data)
            if not upstream:
                self.assertEqual(remote.requests, [])

    async def test_feature_replies_are_local_empty_and_never_query_unleased_device(self):
        for service in (b"host:features", b"host-serial:phone:features"):
            writer, remote = await exchange(pack_message(service))
            self.assertEqual(writer.data, b"OKAY0000")
            self.assertEqual(remote.requests, [])

    @unittest.skipUnless(os.environ.get("R1_ADB"), "set R1_ADB for the exact pinned-client fixture")
    async def test_pinned_stock_adb_only_talks_to_fake_endpoint(self):
        from artemis.runtime.host_peer import SocketRemote
        from tests.support.fake_adb import FakeAdbServer, FakeDevice

        binary = os.environ["R1_ADB"]
        proc = await asyncio.create_subprocess_exec(
            binary, "version", stdout=asyncio.subprocess.PIPE
        )
        version, _ = await proc.communicate()
        self.assertIn(b"37.0.0-14910828", version)
        upstream = FakeAdbServer("r1-fixture")
        upstream.add_device("phone")
        upstream.add_device("hidden")
        upstream.start()
        writers, tasks = set(), set()

        async def client(reader, writer):
            tasks.add(asyncio.current_task())
            writers.add(writer)
            remote_writer = None
            try:
                remote_reader, remote_writer = await asyncio.open_connection(
                    "127.0.0.1", upstream.port
                )
                await LeaseGateway(lambda: {"phone"}).relay(
                    reader, writer, SocketRemote(remote_reader, remote_writer)
                )
            finally:
                if remote_writer is not None:
                    remote_writer.close()
                    await remote_writer.wait_closed()
                writer.close()
                await writer.wait_closed()
                writers.discard(writer)
                tasks.discard(asyncio.current_task())

        server = await asyncio.start_server(client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def run(*args):
            # Explicit private fake destination; never default-port fallback or server startup.
            process = await asyncio.create_subprocess_exec(
                binary,
                "-H",
                "127.0.0.1",
                "-P",
                str(port),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(process.communicate(), 5)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            self.assertEqual(process.returncode, 0, err.decode())
            return out

        try:
            devices = await run("devices")
            self.assertIn(b"phone", devices)
            self.assertNotIn(b"hidden", devices)
            self.assertIn(
                b"r1-positive", await run("-s", "phone", "exec-out", "echo", "r1-positive")
            )
            self.assertIn(b"r1-shell", await run("-s", "phone", "shell", "echo", "r1-shell"))
            self.assertNotIn("host:features", [r.request for r in upstream.requests])
        finally:
            server.close()
            await server.wait_closed()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(upstream.stop)


def key(n):
    name = b"ssh-ed25519"
    blob = struct.pack("!I", len(name)) + name + struct.pack("!I", 32) + bytes([n]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


class SSH:
    """Ops-owned isolation/supervisor fixture; never opens a host SSH session."""

    def __init__(self):
        self.keys = {}
        self.stopped = []
        self.retired = []
        self.stop_ok = True
        self.port = 23000

    async def reserve(self, binding):
        self.port += 1
        return self.port

    async def publish(self, binding, line):
        self.keys[binding] = line
        return True

    async def revoke(self, binding):
        self.keys.pop(binding, None)
        return True

    async def stop(self, session):
        self.stopped.append(session)
        return self.stop_ok

    async def retire(self, binding, port):
        self.retired.append(port)
        return True


class Listener:
    def close(self):
        pass

    async def wait_closed(self):
        pass


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        real_connect = sqlite3.connect

        def connect_without_flush(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.execute("PRAGMA synchronous=OFF")
            return conn

        self.flush_patch = patch("sqlite3.connect", side_effect=connect_without_flush)
        self.flush_patch.start()
        self.addCleanup(self.flush_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.db"
        self.now = 1000.0
        self.snapshot = dict(
            principal="principal",
            agent="agent",
            issue="CHE-fixture",
            run="run",
            lease="lease-1",
            devices=(("canonical", "phone", "mapping-1"),),
            expires=1300.0,
            generation=1,
            profile="reviewed-profile-digest",
            hard_deadline=1400.0,
            baseline="independent-attestation-digest",
            isolation="ops-isolation-digest",
            granted=True,
            restricted=False,
            state="active",
            live=True,
        )
        self.identity = {
            k: self.snapshot[k] for k in ("principal", "agent", "issue", "run", "lease")
        }
        self.ops = SSH()
        self.quarantined = []
        self.remotes = []

        async def listen(callback, host, port, **kwargs):
            self.assertEqual(host, "127.0.0.1")
            return Listener()

        async def connect():
            remote = Remote()
            self.remotes.append(remote)
            return remote

        self.access = RemoteAccess(
            self.path,
            resolve=lambda identity: dict(self.snapshot),
            ops=self.ops,
            quarantine=lambda lease: self.quarantined.append(lease),
            clock=lambda: self.now,
            listen=listen,
            connect=connect,
            bootstrap=True,
        )

    async def issue(self, n=1):
        result = await self.access.issue(self.identity, key(n))
        self.assertIsNotNone(result, "an authorized live lease must get a per-run credential")
        return result

    async def session(self, n=1):
        binding = await self.issue(n)
        session = dict(pid=123 + n, start="start-1", boot="boot-1", binding=binding["id"])
        self.assertTrue(await self.access.authenticate(self.identity, key(n), session))
        return binding, session

    async def asyncTearDown(self):
        if hasattr(self.access, "close"):
            await self.access.close()

    async def test_stale_key_after_reacquire_and_durable_fingerprint_replay(self):
        old, session = await self.session()
        await self.access.fence(old["id"])
        self.snapshot["lease"] = self.identity["lease"] = "lease-2"
        new, _ = await self.session(2)
        self.assertNotEqual(old["id"], new["id"])
        self.assertFalse(await self.access.authenticate(self.identity, key(1), session))
        self.assertIsNone(await self.access.issue(self.identity, key(1)))
        self.assertIn(session, self.ops.stopped)
        self.assertFalse(self.access.valid(old["id"]))

    async def test_concurrent_old_key_registration_cannot_race_fence_or_reacquire(self):
        old, _ = await self.session()
        results = await asyncio.gather(
            self.access.fence(old["id"]),
            *(self.access.issue(self.identity, key(1)) for _ in range(8)),
        )
        self.assertIs(results[0], True)
        self.assertEqual(results[1:], [None] * 8)
        self.snapshot["lease"] = self.identity["lease"] = "lease-2"
        fresh = await self.issue(2)
        self.assertNotEqual(old["id"], fresh["id"])
        self.assertEqual(
            await asyncio.gather(*(self.access.issue(self.identity, key(1)) for _ in range(8))),
            [None] * 8,
        )
        with self.access.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM r1_keys").fetchone()[0], 2)

    async def test_restart_clock_jumps_never_bypass_session_reconciliation(self):
        _, session = await self.session()
        for now in (900, 1500):
            self.now = now
            other = RemoteAccess(
                self.path,
                resolve=lambda identity: dict(self.snapshot),
                ops=self.ops,
                quarantine=lambda lease: self.quarantined.append(lease),
                clock=lambda: self.now,
            )
            try:
                self.assertIsNone(await other.issue(self.identity, key(2)))
                self.assertTrue(await other.reconcile())
                self.assertIn(session, self.ops.stopped)
                self.assertIsNone(await other.issue(self.identity, key(1)))
            finally:
                await other.close()

    async def test_reused_pid_and_uncertain_destination_cannot_be_retired_or_reissued(self):
        first, session = await self.session()
        current_process = dict(session, start="reused-pid-new-start")
        killed = []

        async def identity_checked_stop(recorded):
            self.assertEqual(recorded, session, "owner receives the durable original identity")
            if recorded != current_process:
                return False  # conservative adapter: closure cannot be confirmed
            killed.append(current_process)
            return True

        self.ops.stop = identity_checked_stop
        self.assertFalse(await self.access.fence(first["id"]))
        self.assertEqual(killed, [])
        self.assertNotIn(first["port"], self.ops.retired)
        self.snapshot["lease"] = self.identity["lease"] = "lease-2"
        self.ops.port = first["port"] - 1  # faulty reserve attempts the still-owned destination
        self.assertIsNone(await self.access.issue(self.identity, key(2)))
        self.assertNotIn(first["port"], self.ops.retired)
        self.assertIn("lease-1", self.quarantined)

    async def test_nat_shared_node_missing_wrong_or_other_run_key_never_authenticates(self):
        binding, session = await self.session()
        for credential in ("", "lease-1", key(2)):
            self.assertFalse(await self.access.authenticate(self.identity, credential, session))
        other = dict(self.identity, run="other-run")
        self.assertFalse(await self.access.authenticate(other, key(1), session))
        self.assertTrue(await self.access.authenticate(self.identity, key(1), session))

    async def test_all_binding_fields_and_policy_changes_invalidate(self):
        for n, (name, value) in enumerate(
            (
                ("principal", "other"),
                ("agent", "other"),
                ("issue", "other"),
                ("run", "other"),
                ("lease", "other"),
                ("generation", 2),
                ("restricted", True),
                ("restricted", None),
                ("granted", False),
                ("state", "fencing"),
                ("live", False),
                ("profile", ""),
                ("baseline", "changed"),
                ("isolation", ""),
                ("devices", (("canonical", "wireless:5555", "mapping-2"),)),
            ),
            1,
        ):
            binding = await self.issue(n)
            before = self.snapshot[name]
            self.snapshot[name] = value
            self.assertFalse(self.access.valid(binding["id"]), name)
            self.snapshot[name] = before
            await self.access.fence(binding["id"])

    async def test_restricted_unknown_unattested_and_ungranted_devices_get_no_listener(self):
        await self.issue()
        for name, value in (
            ("restricted", True),
            ("restricted", None),
            ("granted", False),
            ("profile", ""),
            ("baseline", ""),
            ("isolation", ""),
        ):
            before = self.snapshot[name]
            self.snapshot[name] = value
            self.assertIsNone(await self.access.issue(self.identity, key(2)), name)
            self.snapshot[name] = before
        self.assertEqual(len(self.ops.keys), 1)

    async def test_ssh_key_has_only_its_destination_and_no_raw_adb(self):
        binding = await self.issue()
        line = self.ops.keys[binding["id"]]
        self.assertIn(f'permitopen="127.0.0.1:{binding["port"]}"', line)
        self.assertIn("restrict,port-forwarding", line)
        self.assertNotIn(":5037", line)
        self.assertNotIn("*", line)
        self.assertIsNone(await self.access.issue(self.identity, 'command="id" ' + key(2)))

    async def test_release_closes_selected_unselected_discovery_wait_and_all_sessions(self):
        binding, session = await self.session()
        writers, tasks = [], []
        for prefix in (
            b"",
            pack_message(b"host:track-devices"),
            pack_message(b"host:transport:phone"),
            pack_message(b"host:transport:phone") + pack_message(b"shell:id"),
        ):
            reader, writer = asyncio.StreamReader(), Writer()
            reader.feed_data(prefix)
            writers.append(writer)
            tasks.append(asyncio.create_task(self.access.client(binding["id"], reader, writer)))
        for _ in range(10):
            await asyncio.sleep(0)
        await self.access.fence(binding["id"])
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(w.closed for w in writers))
        self.assertIn(session, self.ops.stopped)
        self.assertEqual(self.access.tunnels[binding["id"]].tasks, set())
        writer = Writer()
        await self.access.client(binding["id"], asyncio.StreamReader(), writer)
        self.assertTrue(writer.closed)
        self.assertFalse(await self.access.authenticate(self.identity, key(1), session))

    async def test_expiry_authority_and_control_loss_close_every_stream(self):
        for n, mutation in enumerate(("expiry", "generation", "control", "restriction"), 1):
            self.snapshot.update(state="active", generation=1, restricted=False, live=True)
            self.now = 1000
            binding, session = await self.session(n)
            if mutation == "expiry":
                self.now = 1300
            elif mutation == "generation":
                self.snapshot["generation"] = 2
            elif mutation == "restriction":
                self.snapshot["restricted"] = True
            else:
                self.snapshot["live"] = False
            await self.access.maintain()
            self.assertFalse(self.access.valid(binding["id"]))
            self.assertIn(session, self.ops.stopped)

    async def test_restart_stops_recorded_sessions_before_new_key_and_keeps_tombstones(self):
        binding, session = await self.session()
        # A second owner simulates process death; no in-memory sessions are imported.
        other = RemoteAccess(
            self.path,
            resolve=lambda identity: dict(self.snapshot),
            ops=self.ops,
            quarantine=lambda lease: self.quarantined.append(lease),
            clock=lambda: self.now,
        )
        self.assertIsNone(await other.issue(self.identity, key(2)))
        await other.reconcile()
        self.assertIn(session, self.ops.stopped)
        self.assertIsNone(await other.issue(self.identity, key(1)))
        await other.close()

    async def test_unverified_session_stop_quarantines_and_does_not_retire_port(self):
        binding, _ = await self.session()
        self.ops.stop_ok = False
        self.assertFalse(await self.access.fence(binding["id"]))
        self.assertIn("lease-1", self.quarantined)
        self.assertNotIn(binding["port"], self.ops.retired)

    async def test_monitor_survives_quarantine_failure_and_fences_another_lease(self):
        first, session1 = await self.session()
        snapshots = {self.identity["lease"]: dict(self.snapshot)}
        self.snapshot["lease"] = self.identity["lease"] = "lease-2"
        snapshots["lease-2"] = self.snapshot
        self.access.resolve = lambda identity: dict(snapshots[identity["lease"]])
        second, session2 = await self.session(2)
        original = self.access.quarantine
        stop = self.ops.stop

        async def selective_stop(session):
            await stop(session)
            return session != session1

        def broken_quarantine(lease):
            raise OSError("control unavailable")

        self.ops.stop = selective_stop
        self.access.quarantine = broken_quarantine
        try:
            snapshots["lease-1"]["live"] = False
            await asyncio.sleep(0.12)
            self.assertFalse(
                self.access.monitor.done(), "one failed quarantine cannot stop monitoring"
            )
            self.assertFalse(self.access.ready, "unconfirmed quarantine closes admission")
            snapshots["lease-2"]["live"] = False
            await asyncio.sleep(0.12)
            self.assertIn(session2, self.ops.stopped)
            self.assertNotIn(second["id"], self.ops.keys)
            self.assertFalse(self.access.valid(first["id"]))
        finally:
            self.ops.stop = stop
            self.access.quarantine = original

    async def test_database_finalize_failure_reports_unconfirmed_fence(self):
        from contextlib import contextmanager

        binding, _ = await self.session()
        original = self.access.db

        @contextmanager
        def failing_db():
            with original() as conn:

                class Broken:
                    def execute(self, sql, *args):
                        if "phase='closed'" in sql:
                            raise sqlite3.OperationalError("commit unavailable")
                        return conn.execute(sql, *args)

                yield Broken()

        with patch.object(self.access, "db", failing_db):
            try:
                result = await self.access.fence(binding["id"])
            except sqlite3.Error:
                result = "raised"
        self.assertIs(result, False, "failed durable close must quarantine and report failure")
        self.assertFalse(self.access.ready)
        self.assertIn("lease-1", self.quarantined)

    async def test_observed_policy_loss_never_revives_a_credential(self):
        binding = await self.issue()
        self.snapshot["restricted"] = True
        self.assertFalse(self.access.valid(binding["id"]))
        self.snapshot["restricted"] = False
        self.assertFalse(self.access.valid(binding["id"]), "fresh admission requires a new key")

    async def test_expiry_cannot_exceed_hard_cap(self):
        await self.issue()
        self.snapshot["lease"] = self.identity["lease"] = "lease-2"
        self.snapshot["expires"] = 1401
        self.assertIsNone(await self.access.issue(self.identity, key(2)))

    async def test_world_readable_state_directory_denies_new_credentials(self):
        await self.issue()
        self.path.parent.chmod(0o755)
        try:
            self.snapshot["lease"] = self.identity["lease"] = "lease-2"
            self.assertIsNone(await self.access.issue(self.identity, key(2)))
        finally:
            self.path.parent.chmod(0o700)

    async def test_lost_history_still_closes_local_streams_and_quarantines(self):
        binding, _ = await self.session()
        reader, writer = asyncio.StreamReader(), Writer()
        task = asyncio.create_task(self.access.client(binding["id"], reader, writer))
        for _ in range(10):
            await asyncio.sleep(0)
        with patch.object(self.access, "db", side_effect=sqlite3.OperationalError("unavailable")):
            try:
                await self.access.fence(binding["id"])
            except sqlite3.Error:
                pass
            self.assertTrue(writer.closed)
            self.assertIn("lease-1", self.quarantined)
        await asyncio.gather(task, return_exceptions=True)

    async def test_tunnel_budgets_return_to_zero_after_fencing(self):
        from artemis.runtime.host_protocol import CONTRACT
        from artemis.runtime.host_mux import PROCESS_BUDGET

        start = PROCESS_BUDGET.used
        binding, _ = await self.session()
        tunnel = self.access.tunnels[binding["id"]]
        writers, tasks = [], []
        for _ in range(CONTRACT.max_streams + 1):
            writer = Writer()
            writers.append(writer)
            tasks.append(
                asyncio.create_task(
                    self.access.client(binding["id"], asyncio.StreamReader(), writer)
                )
            )
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertLessEqual(tunnel.budget.used, CONTRACT.host_budget - CONTRACT.control_reserve)
        self.assertTrue(any(w.closed for w in writers))
        await self.access.fence(binding["id"])
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(tunnel.budget.used, 0)
        self.assertEqual(PROCESS_BUDGET.used, start)

    async def test_failed_socket_close_quarantines_and_still_releases_budget(self):
        class FailedClose(Writer):
            async def wait_closed(self):
                raise OSError("unconfirmed close")

        binding, _ = await self.session()
        tunnel = self.access.tunnels[binding["id"]]
        writer = FailedClose()
        task = asyncio.create_task(
            self.access.client(binding["id"], asyncio.StreamReader(), writer)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertFalse(await self.access.fence(binding["id"]))
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(tunnel.budget.used, 0)
        self.assertIn("lease-1", self.quarantined)

    async def test_restricted_profile_denies_socket_bypasses(self):
        for service in (b"tcp:1234", b"localabstract:private"):
            writer, remote = await exchange(
                pack_message(b"host:transport:phone") + pack_message(service)
            )
            self.assertIn(b"FAIL", writer.data)
            self.assertEqual(remote.requests, [pack_message(b"host:transport:phone")])

    async def test_denial_never_logs_shell_or_path_payload(self):
        with self.assertLogs("artemis.runtime.adb_gateway", level="WARNING") as logs:
            writer, remote = await exchange(pack_message(b"host:connect:SECRET-MARKER"))
        self.assertIn(b"FAIL", writer.data)
        self.assertEqual(remote.requests, [])
        self.assertNotIn("SECRET-MARKER", " ".join(logs.output))

    async def test_valid_fragments_coalescing_and_tport_bytes(self):
        payload = pack_message(b"host:tport:serial:phone") + pack_message(b"exec:id")
        for boundary in range(1, len(payload)):
            reader, writer, remote = asyncio.StreamReader(), Writer(), Remote()
            task = asyncio.create_task(
                LeaseGateway(lambda: {"phone"}).relay(reader, writer, remote)
            )
            reader.feed_data(payload[:boundary])
            await asyncio.sleep(0)
            reader.feed_data(payload[boundary:])
            reader.feed_eof()
            await asyncio.wait_for(task, 1)
            self.assertEqual(writer.data, b"OKAY12345678OKAYoutput")
            self.assertEqual(len(remote.requests), 2)

    async def test_malformed_frames_never_partially_dispatch(self):
        for payload in (b"GGGG", b"ffffshort", b"0000", b"0002\xff\xff", b"0001\0"):
            writer, remote = await exchange(payload)
            self.assertTrue(writer.data.startswith(b"FAIL"))
            self.assertEqual(remote.requests, [])

    async def test_global_controls_and_unleased_selectors_are_denied(self):
        for service in (
            "host:kill",
            "host:connect:x",
            "host:disconnect:x",
            "host:transport-any",
            "host:transport-usb",
            "host:transport-local",
            "host:transport-id:1",
            "host:tport:any",
            "host:tport:usb",
            "host:tport:local",
            "host:transport:hidden",
            "host:forward:tcp:1;tcp:2",
            "host-serial:phone:wait-for-any-device:injected",
            "unknown",
        ):
            writer, remote = await exchange(pack_message(service.encode()))
            self.assertTrue(writer.data.startswith(b"FAIL"), service)
            self.assertEqual(remote.requests, [])


if __name__ == "__main__":
    unittest.main()
