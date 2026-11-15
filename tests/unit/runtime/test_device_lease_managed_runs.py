"""Managed launcher contract, fake inventory only; no ADB or live broker."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_device_lease_broker as fixtures
import daemon
from broker import process_identity
from daemon import BrokerServer
from managed_runs import ManagedRuns


class ManagedRunTests(unittest.TestCase):
    setUpFixtures = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take
    refused = fixtures.BrokerTests.refused

    def setUp(self):
        self.setUpFixtures()
        self.directory = Path(self.temp.name) / "bindings"
        self.directory.mkdir(mode=0o700)
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
        )
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.stdin.close)
        self.binding_id = "a" * 64
        self.record = dict(process_identity(self.child.pid))
        self.record.pop("mode")
        self.record.update(
            version=1,
            binding_id=self.binding_id,
            agent_id="agent",
            principal="agent",
            task_id="run-1",
            issue_id="CHE-fixture",
            pid_namespace=os.readlink("/proc/self/ns/pid"),
        )
        self.write_record()
        self.broker.managed_runs = ManagedRuns(self.directory)
        self.binding = {"mode": "managed", "binding_id": self.binding_id}

    def write_record(self):
        self.record_path = self.directory / (self.binding_id + ".json")
        self.record_path.write_text(json.dumps(self.record))
        self.record_path.chmod(0o600)

    def test_managed_binding_resolves_host_identity_and_revocation_fences(self):
        with patch("daemon.process_identity", side_effect=AssertionError("namespace PID lookup")):
            with BrokerServer(Path(self.temp.name) / "socket", self.broker) as server:
                group = server.dispatch(
                    {
                        "op": "acquire",
                        "args": dict(
                            principal="agent",
                            agent_id="agent",
                            issue_id="CHE-fixture",
                            run_id="run-1",
                            purpose="CHE-fixture",
                            idempotency_key="managed",
                            binding=self.binding,
                        ),
                    }
                )
        self.assertEqual(group["state"], "active")
        with sqlite3.connect(self.path) as conn:
            stored = json.loads(conn.execute("SELECT binding FROM broker_bindings").fetchone()[0])
        self.assertEqual(stored["pid"], self.child.pid)
        self.assertEqual(stored["task_id"], "run-1")
        self.record_path.unlink()
        self.broker.maintain()
        status = self.broker.status(lease_id=group["lease_id"], principal="agent", run_id="run-1")
        self.assertEqual(status["state"], "fencing")

    def test_cli_passes_only_reference_without_namespace_pid_lookup(self):
        common = [
            "--principal",
            "agent",
            "--agent-id",
            "agent",
            "--issue-id",
            "CHE-fixture",
            "--run-id",
            "run-1",
            "--managed-binding",
            self.binding_id,
            "--idempotency-key",
            "cli",
        ]
        for op, extra in (
            ("acquire", ["--purpose", "CHE-fixture"]),
            ("claim", ["--ticket", "ticket-1"]),
        ):
            with (
                self.subTest(op=op),
                patch("daemon.rpc", return_value={}) as rpc,
                patch("daemon.process_identity", side_effect=AssertionError("namespace /proc")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(daemon.main([op, *common, *extra]), 0)
                self.assertEqual(rpc.call_args.args[1]["args"]["binding"], self.binding)

    def test_wrong_task_principal_agent_issue_and_reference_fail_closed(self):
        for changes in (
            {"run_id": "other-run"},
            {"principal": "other"},
            {"agent_id": "other"},
            {"issue_id": "CHE-other"},
        ):
            with self.subTest(changes=changes):
                self.refused("run_not_live", lambda: self.take(binding=self.binding, **changes))
        for binding in (
            {"mode": "managed", "binding_id": "../outside"},
            {"mode": "managed", "binding_id": "b" * 64},
        ):
            self.refused("run_not_live", lambda: self.take(binding=binding))
        self.refused("invalid_request", lambda: self.take(binding={**self.binding, "pid": 1}))
        unconfigured = self.new_broker()
        self.refused(
            "invalid_request", lambda: self.take(broker=unconfigured, binding=self.binding)
        )

    def test_namespace_boot_start_and_dead_holder_fail_closed(self):
        original = dict(self.record)
        for changes in (
            {"pid": 1},
            {"pid_namespace": "pid:[other]"},
            {"boot_id": "other-boot"},
            {"process_start": "0"},
            {"pid": 999999999},
        ):
            with self.subTest(changes=changes):
                self.record = {**original, **changes}
                self.write_record()
                self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.record = original
        self.write_record()
        self.child.stdin.close()
        self.child.wait()
        self.refused("run_not_live", lambda: self.take(binding=self.binding))

    def test_pid_reuse_record_replacement_fences_active_group(self):
        group = self.take(binding=self.binding)
        self.record["process_start"] = "0"
        self.write_record()
        self.broker.maintain()
        self.assertEqual(
            self.broker.status(lease_id=group["lease_id"], principal="agent", run_id="run-1")[
                "state"
            ],
            "fencing",
        )

    def test_record_and_registry_trust_boundaries(self):
        for mode in (0o644, 0o666):
            self.record_path.chmod(mode)
            self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.record_path.chmod(0o600)
        target = self.directory / "target.json"
        self.record_path.rename(target)
        self.record_path.symlink_to(target)
        self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.record_path.unlink()
        self.write_record()
        self.record_path.write_text("{bad JSON")
        self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.record_path.write_bytes(b"x" * 8193)
        self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.write_record()
        self.directory.chmod(0o755)
        self.refused("run_not_live", lambda: self.take(binding=self.binding))
        self.directory.chmod(0o700)
        self.directory.rename(self.directory.with_name("old-registry"))
        self.directory.mkdir(mode=0o700)
        self.write_record()
        self.refused("run_not_live", lambda: self.take(binding=self.binding))

    def test_managed_queue_parks_on_revocation_and_claim_checks_new_run(self):
        holder = self.take(run_id="holder", idempotency_key="holder", device_id="a")
        ticket = self.refused(
            "device_busy",
            lambda: self.take(binding=self.binding, idempotency_key="queued", device_id="a"),
        ).details["ticket"]
        self.record_path.unlink()
        self.broker.maintain()
        with sqlite3.connect(self.path) as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT binding FROM broker_ticket_wait WHERE ticket_id=?",
                    (ticket["ticket_id"],),
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE broker_groups SET state='released' WHERE lease_id=?", (holder["lease_id"],)
            )
        self.record["task_id"] = "run-2"
        self.write_record()
        self.broker.maintain()
        self.refused(
            "run_not_live",
            lambda: self.broker.claim(
                ticket_id=ticket["ticket_id"],
                principal="agent",
                agent_id="agent",
                issue_id="CHE-fixture",
                run_id="wrong",
                binding=self.binding,
                idempotency_key="wrong",
            ),
        )
        claimed = self.broker.claim(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id="CHE-fixture",
            run_id="run-2",
            binding=self.binding,
            idempotency_key="claim",
        )
        # A parked run may not reclaim its old offer; claim in a distinct task.
        self.assertEqual(claimed["state"], "active")


if __name__ == "__main__":
    unittest.main()
