"""DLP-1 queue behavior, with synthetic inventory and no device commands."""

import sqlite3
from pathlib import Path
import os
import unittest
import test_device_lease_broker as fixtures
from daemon import BrokerServer


class QueueTests(unittest.TestCase):
    setUp = fixtures.BrokerTests.setUp
    new_broker = fixtures.BrokerTests.new_broker
    take = fixtures.BrokerTests.take
    refused = fixtures.BrokerTests.refused

    def busy(self, **changes):
        error = self.refused("device_busy", lambda: self.take(**changes))
        self.assertIn("ticket", error.details, "busy acquire must persist a queue ticket")
        return error.details["ticket"]

    def ticket(self, ticket):
        return self.broker.ticket_status(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id=ticket["issue_id"],
        )

    def free(self, group):
        # Simulate the future leader-verified release; never expose this as RPC.
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE broker_groups SET state='released' WHERE lease_id=?", (group["lease_id"],)
            )

    def wait(self, run, **changes):
        return self.busy(run_id=run, purpose=f"CHE-{run}", idempotency_key=run, **changes)

    def test_cancelled_ticket_frees_its_place_and_cannot_cancel_another_owner(self):
        holder = self.take(device_id="a")
        first = self.wait("first", device_id="a")
        second = self.wait("second", device_id="a")
        self.refused(
            "ticket_not_yours",
            lambda: self.broker.cancel_ticket(
                ticket_id=first["ticket_id"],
                principal="other",
                agent_id="agent",
                issue_id=first["issue_id"],
            ),
        )
        self.broker.cancel_ticket(
            ticket_id=first["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id=first["issue_id"],
        )
        self.free(holder)
        self.assertEqual(self.ticket(second)["state"], "offered")
        self.assertEqual(self.ticket(first)["state"], "cancelled")

    def test_abandoned_offer_passes_to_next_ticket_without_granting_access(self):
        holder = self.take(device_id="a")
        first = self.wait("first", device_id="a")
        second = self.wait("second", device_id="a")
        self.free(holder)
        offer = self.ticket(first)
        self.assertEqual(offer["state"], "offered")
        self.assertIsNone(offer["offer_expires_at"], "window needs signal read-back")
        self.broker.start_offer_window(first["ticket_id"], offer["offer_sequence"])
        self.now += 900
        self.assertEqual(self.ticket(second)["state"], "offered")
        self.assertEqual(self.ticket(first)["state"], "queued")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM broker_groups WHERE state='active'").fetchone()[
                    0
                ],
                0,
            )

    def test_exact_and_pool_fifo_conflicts_but_disjoint_device_passes(self):
        holder = self.take(device_id="a")
        other = self.take(
            run_id="other-holder",
            purpose="CHE-other-holder",
            idempotency_key="other-holder",
            device_id="b",
        )
        first = self.wait("pool")
        self.assertEqual(first["state"], "queued")
        self.free(other)
        self.assertEqual(self.ticket(first)["state"], "offered")
        exact = self.wait("exact", device_id="a")
        self.free(holder)
        self.assertEqual(self.ticket(exact)["state"], "queued")
        root = self.take(
            run_id="disjoint", purpose="CHE-disjoint", pool="root", idempotency_key="disjoint"
        )
        self.assertEqual(root["state"], "active")

    def test_head_batch_reserves_free_members_and_no_later_exact_can_pass(self):
        holder = self.take(device_id="a")
        batch = self.wait("batch", count=2)
        later = self.wait("later", device_id="b")
        self.assertEqual(self.ticket(batch)["reserved"], ["b"])
        self.assertEqual(self.ticket(later)["state"], "queued")
        self.free(holder)
        self.assertEqual(self.ticket(batch)["reserved"], ["a", "b"])
        self.assertEqual(self.ticket(batch)["state"], "offered")
        self.assertEqual(self.ticket(later)["reserved"], [])

    def test_ticket_survives_reopen_and_one_open_ticket_per_issue(self):
        self.take(count=2)
        first = self.wait("wait", count=2)
        retry = self.busy(run_id="successor", purpose="CHE-wait", count=1, idempotency_key="retry")
        self.assertEqual(first["ticket_id"], retry["ticket_id"])
        self.assertEqual(retry["count"], 2)
        self.broker = self.new_broker()
        self.assertEqual(self.ticket(first)["ticket_id"], first["ticket_id"])

    def test_local_binding_is_persisted_and_pid_reuse_fences_instead_of_freeing(self):
        holder = self.take()
        self.assertIn("binding", holder, "grant must record PID, start time, boot id and run id")
        binding = holder["binding"]
        self.assertEqual(binding["run_id"], "run-1")
        self.assertEqual(binding["pid"], os.getpid())
        self.assertTrue(binding["process_start"])
        self.assertTrue(binding["boot_id"])
        self.broker.run_probe = lambda _: False  # same PID, different start/boot, or dead
        status = self.broker.status(lease_id=holder["lease_id"], principal="agent", run_id="run-1")
        self.assertEqual(status["state"], "fencing")
        self.busy(
            broker=self.new_broker(),
            run_id="new",
            purpose="CHE-new",
            idempotency_key="new",
            device_id="a",
        )

    def test_restart_fences_every_lease_and_blocks_disjoint_grant_until_reconciled(self):
        holder = self.take()
        with BrokerServer(Path(self.temp.name) / "state", self.broker):
            status = self.broker.status(
                lease_id=holder["lease_id"], principal="agent", run_id="run-1"
            )
            self.assertEqual(status["state"], "fencing", "restart must fence old active leases")
            self.refused(
                "restart_reconcile",
                lambda: self.take(
                    run_id="new", purpose="CHE-new", pool="root", idempotency_key="new"
                ),
            )
            self.refused(
                "reconcile_incomplete", lambda: self.broker.finish_reconcile(holder["lease_id"])
            )
            self.broker.recovery(
                holder["lease_id"],
                "fencing",
                "quarantine",
                recovery_owner="ops",
                ops_issue="CHE-ops",
            )
            self.broker.finish_reconcile(holder["lease_id"])
            self.assertEqual(
                self.take(run_id="new", purpose="CHE-new", pool="root", idempotency_key="new")[
                    "state"
                ],
                "active",
            )

    def test_clock_jump_fences_before_any_new_grant(self):
        self.broker.monotonic = lambda: 100.0
        holder = self.take()
        self.now += 60  # wall clock jumped; monotonic did not
        self.refused(
            "restart_reconcile",
            lambda: self.take(run_id="new", purpose="CHE-new", pool="root", idempotency_key="new"),
        )
        status = self.broker.status(lease_id=holder["lease_id"], principal="agent", run_id="run-1")
        self.assertEqual(status["state"], "fencing")

    def test_transport_requires_client_binding_and_refuses_remote_identity(self):
        with BrokerServer(Path(self.temp.name) / "state", self.broker) as server:
            request = dict(
                principal="agent", run_id="run", purpose="CHE-rpc", idempotency_key="rpc"
            )
            self.refused(
                "invalid_request", lambda: server.dispatch({"op": "acquire", "args": request})
            )
            request["binding"] = {"mode": "remote"}
            self.refused(
                "invalid_request", lambda: server.dispatch({"op": "acquire", "args": request})
            )

    def test_idle_daemon_checks_heartbeat_without_a_client_request(self):
        with BrokerServer(Path(self.temp.name) / "state", self.broker) as server:
            holder = self.take(binding={"mode": "remote"})
            self.now += 30
            self.broker.heartbeat(lease_id=holder["lease_id"], principal="agent", run_id="run-1")
            self.now += 89
            server.service_actions()
            with sqlite3.connect(self.path) as conn:
                self.assertEqual(
                    conn.execute("SELECT state FROM broker_groups").fetchone()[0], "active"
                )
            self.now += 1
            server.service_actions()
            with sqlite3.connect(self.path) as conn:
                self.assertEqual(
                    conn.execute("SELECT state FROM broker_groups").fetchone()[0], "fencing"
                )
            self.refused(
                "lease_not_active",
                lambda: self.broker.heartbeat(
                    lease_id=holder["lease_id"], principal="agent", run_id="run-1"
                ),
            )

    def test_clock_jump_is_still_detected_after_an_interrupted_transaction(self):
        self.broker.monotonic = lambda: 100.0
        holder = self.take()
        self.now += 60
        with self.assertRaises(KeyboardInterrupt):
            with self.broker.transaction():
                raise KeyboardInterrupt()
        self.refused(
            "restart_reconcile",
            lambda: self.take(run_id="new", purpose="CHE-new", pool="root", idempotency_key="new"),
        )
        self.assertEqual(
            self.broker.status(lease_id=holder["lease_id"], principal="agent", run_id="run-1")[
                "state"
            ],
            "fencing",
        )

    def test_exact_ticket_precedes_pool_and_does_not_block_disjoint_exact(self):
        holder = self.take(device_id="a")
        exact = self.wait("exact", device_id="a")
        free = self.take(
            run_id="disjoint", purpose="CHE-disjoint", device_id="b", idempotency_key="disjoint"
        )
        pool = self.wait("pool")
        self.free(holder)
        self.assertEqual(self.ticket(exact)["state"], "offered")
        self.assertEqual(self.ticket(pool)["state"], "queued")
        self.free(free)
        self.assertEqual(self.ticket(pool)["reserved"], [])

    def test_offer_keeps_its_device_selection_when_another_device_frees(self):
        first = self.take(device_id="a")
        second = self.take(
            run_id="other", purpose="CHE-other", device_id="b", idempotency_key="other"
        )
        waiting = self.wait("pool")
        self.free(second)
        offered = self.ticket(waiting)
        self.assertEqual(offered["reserved"], ["b"])
        self.broker.start_offer_window(waiting["ticket_id"], offered["offer_sequence"])
        self.free(first)
        refreshed = self.ticket(waiting)
        self.assertEqual(refreshed["reserved"], ["b"])
        self.assertEqual(refreshed["offer_sequence"], offered["offer_sequence"])

    def test_explicit_empty_agent_or_issue_is_refused(self):
        for change in ({"agent_id": ""}, {"issue_id": ""}):
            with self.subTest(change=change):
                self.refused("invalid_request", lambda: self.take(**change))


if __name__ == "__main__":
    unittest.main()
