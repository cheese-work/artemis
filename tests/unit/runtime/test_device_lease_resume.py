"""Park/resume contract using fake clocks, processes and Multica. No devices."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_device_lease_queue as fixtures
from broker import process_identity
from daemon import BrokerServer


class FakeMultica:
    def __init__(self):
        self.properties = {}
        self.rules = []
        self.comments = []
        self.checkins = []
        self.fail_write = False
        self.wrong_readback = False
        self.calls = []

    def set_offer(self, issue, property_id, value):
        self.calls.append("set")
        if self.fail_write:
            raise OSError("fake failure")
        self.properties[issue, property_id] = value

    def read_offer(self, issue, property_id):
        self.calls.append("read")
        return "old:1" if self.wrong_readback else self.properties.get((issue, property_id))

    def create_wakeup(self, issue, property_id, value):
        self.rules.append((issue, property_id, value, "60m", "wake"))
        return f"wakeup-{len(self.rules)}"

    def fires(self, index=-1):
        issue, prop, value, *_ = self.rules[index]
        return self.properties.get((issue, prop)) == value

    def comment(self, issue, text):
        self.comments.append((issue, text))

    def checkin(self, issue, wakeup_id, text):
        self.checkins.append((issue, wakeup_id, text))


class ResumeTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    new_broker = fixtures.QueueTests.new_broker
    take = fixtures.QueueTests.take
    refused = fixtures.QueueTests.refused
    busy = fixtures.QueueTests.busy
    wait = fixtures.QueueTests.wait
    free = fixtures.QueueTests.free
    ticket = fixtures.QueueTests.ticket

    def owner(self, ticket, **changes):
        args = dict(
            ticket_id=ticket["ticket_id"],
            principal="agent",
            agent_id="agent",
            issue_id=ticket["issue_id"],
        )
        args.update(changes)
        return args

    def park(self, ticket, run_id="wait"):
        return self.broker.park(**self.owner(ticket), run_id=run_id)

    def claim(self, ticket, **changes):
        args = dict(
            self.owner(ticket),
            run_id="successor",
            binding=process_identity(os.getpid()),
            idempotency_key="claim",
        )
        args.update(changes)
        return self.broker.claim(**args)

    def offered(self):
        holder = self.take(device_id="a")
        ticket = self.wait("wait", device_id="a")
        self.park(ticket)
        self.free(holder)
        return self.ticket(ticket)

    def worker(self, client):
        import resume

        return resume.OfferWorker(self.broker, client, "device-offer", monotonic=lambda: self.now)

    def test_park_drops_binding_and_old_run_can_never_claim(self):
        offered = self.offered()
        self.assertIsNone(offered["binding"])
        self.refused("run_parked", lambda: self.claim(offered, run_id="wait"))
        self.refused(
            "run_parked", lambda: self.claim(offered, run_id="wait", idempotency_key="new")
        )

    def test_live_successor_claim_is_atomic_idempotent_and_records_latency(self):
        offered = self.offered()
        self.broker.start_offer_window(offered["ticket_id"], offered["offer_sequence"])
        self.now += 12
        group = self.claim(offered)
        self.assertEqual(group["state"], "active")
        self.assertEqual(group["run_id"], "successor")
        self.assertEqual(group["binding"]["run_id"], "successor")
        self.assertEqual(group["devices"][0]["device_id"], "a")
        self.assertEqual(self.claim(offered), group)
        self.assertEqual(self.ticket(offered)["state"], "claimed")
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT latency_seconds FROM broker_claim_latency").fetchall()
        self.assertEqual(rows, [(12.0,)])
        self.refused("idempotency_conflict", lambda: self.claim(offered, ticket_id="different"))

    def test_claim_refuses_wrong_owner_dead_binding_and_nested_grant(self):
        offered = self.offered()
        for changes in ({"agent_id": "other"}, {"issue_id": "other"}, {"principal": "other"}):
            with self.subTest(changes=changes):
                self.refused("ticket_not_yours", lambda: self.claim(offered, **changes))
        self.broker.run_probe = lambda _: False
        self.refused("run_not_live", lambda: self.claim(offered))
        self.broker.run_probe = lambda _: True
        self.take(run_id="successor", purpose="CHE-another", device_id="b", idempotency_key="held")
        self.refused("run_already_holds", lambda: self.claim(offered))
        self.assertEqual(self.ticket(offered)["state"], "offered")

    def test_claim_requires_an_explicit_binding(self):
        offered = self.offered()
        self.refused("invalid_request", lambda: self.claim(offered, binding=None))

    def test_busy_acquire_cannot_nest(self):
        self.take(run_id="nested", purpose="CHE-nested", device_id="b", idempotency_key="nested")
        self.refused(
            "run_already_holds",
            lambda: self.take(
                run_id="nested", purpose="CHE-second", device_id="b", idempotency_key="second"
            ),
        )

    def test_busy_acquire_does_not_reset_a_spent_run_budget(self):
        first = self.take(device_id="a")
        self.free(first)
        self.now += 7200
        self.take(run_id="holder", purpose="CHE-holder", device_id="a", idempotency_key="holder")
        self.refused("hard_cap", lambda: self.take(device_id="a", idempotency_key="past-cap"))

    def test_dead_waiting_run_is_auto_parked_and_cannot_claim_after_reopen(self):
        holder = self.take(device_id="a")
        ticket = self.wait("wait", device_id="a")
        self.broker.run_probe = lambda _: False
        self.broker.maintain()
        self.free(holder)
        self.broker = self.new_broker()
        self.refused("run_parked", lambda: self.claim(ticket, run_id="wait"))
        self.assertEqual(self.claim(ticket)["run_id"], "successor")

    def test_offer_before_wakeup_fires_and_old_sequence_does_not(self):
        offered = self.offered()
        client = FakeMultica()
        self.worker(client).run_once()
        self.assertEqual(client.calls, ["set", "read"])
        value = f"{offered['ticket_id']}:{offered['offer_sequence']}"
        client.create_wakeup(offered["issue_id"], "device-offer", value)
        self.assertTrue(client.fires(), "a pre-existing offer must satisfy a later condition")
        client.create_wakeup(offered["issue_id"], "device-offer", f"{offered['ticket_id']}:2")
        self.assertFalse(client.fires(), "old sequence cannot satisfy the next condition")

    def test_signal_failure_and_wrong_readback_leave_window_closed_and_retry_with_backoff(self):
        offered = self.offered()
        client = FakeMultica()
        worker = self.worker(client)
        client.fail_write = True
        worker.run_once()
        worker.run_once()
        self.assertEqual(client.calls, ["set"], "no hot retry")
        self.assertIsNone(self.ticket(offered)["offer_expires_at"])
        self.now += 60
        client.fail_write = False
        client.wrong_readback = True
        worker.run_once()
        self.assertIsNone(self.ticket(offered)["offer_expires_at"])
        self.now += 120
        client.wrong_readback = False
        worker.run_once()
        self.assertEqual(self.ticket(offered)["offer_expires_at"], self.now + 900)

    def test_property_io_occurs_outside_writer_transaction_and_stale_ack_cannot_start_new_offer(
        self,
    ):
        offered = self.offered()
        client = FakeMultica()
        original = client.set_offer

        def write(issue, prop, value):
            with sqlite3.connect(self.path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
            original(issue, prop, value)

        client.set_offer = write
        self.worker(client).run_once()
        self.now += 900
        new_offer = self.ticket(offered)
        self.assertEqual(new_offer["offer_sequence"], 2)
        self.refused("offer_stale", lambda: self.broker.start_offer_window(offered["ticket_id"], 1))
        self.assertIsNone(self.ticket(offered)["offer_expires_at"])

    def test_abandon_after_75_min_poll_refreshes_but_property_worker_does_not(self):
        holder = self.take(device_id="a")
        ticket = self.wait("wait", device_id="a")
        self.now += 3600
        self.assertEqual(self.ticket(ticket)["state"], "queued")
        self.now += 4499
        self.broker.maintain()
        self.assertEqual(self.ticket(ticket)["state"], "queued")
        self.now += 4500
        self.assertEqual(self.ticket(ticket)["state"], "cancelled")

    def test_two_missed_offers_cancel_and_release_reservations(self):
        offered = self.offered()
        for sequence in (1, 2):
            self.broker.start_offer_window(offered["ticket_id"], sequence)
            self.now += 900
            result = self.ticket(offered)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["missed_offers"], 2)
        self.assertEqual(result["reserved"], [])
        self.refused("ticket_not_offered", lambda: self.claim(offered))

    def test_timeout_polls_refreshes_creates_one_condition_and_one_line_comment(self):
        import resume

        self.take(device_id="a")
        ticket = self.wait("wait", device_id="a")
        self.park(ticket)
        client = FakeMultica()
        self.now += 3600
        result = resume.fallback_wait(self.broker, client, "device-offer", **self.owner(ticket))
        self.assertEqual(result["action"], "wait")
        self.assertEqual(
            client.rules,
            [(ticket["issue_id"], "device-offer", f"{ticket['ticket_id']}:1", "60m", "wake")],
        )
        self.assertEqual(len(client.comments), 1)
        self.assertNotIn("\n", client.comments[0][1])
        self.assertIn("wakeup-1", client.comments[0][1])
        self.assertEqual(client.checkins, [])
        self.now += 4499
        self.assertEqual(self.ticket(ticket)["state"], "queued")

    def test_checkin_requires_actual_wakeup_block_offer(self):
        import resume

        self.take(device_id="a")
        ticket = self.wait("wait", device_id="a")
        self.park(ticket)
        client = FakeMultica()
        resume.fallback_wait(
            self.broker,
            client,
            "device-offer",
            **self.owner(ticket),
            wakeup_block=f"[WAKEUP]\nmultica issue wakeup checkin {ticket['issue_id']} actual-rule\n[/WAKEUP]",
        )
        self.assertEqual(len(client.checkins), 1)
        self.assertEqual(client.checkins[0][1], "actual-rule")
        self.assertEqual(client.comments, [])

    def test_claim_interruption_rolls_back_ticket_group_and_latency(self):
        offered = self.offered()
        self.broker.start_offer_window(offered["ticket_id"], 1)

        def interrupt(name):
            if name == "acquire:device":
                raise KeyboardInterrupt()

        self.broker.boundary = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.claim(offered)
        self.assertEqual(self.ticket(offered)["state"], "offered")
        self.broker.boundary = lambda _: None
        self.assertEqual(self.claim(offered)["state"], "active")

    def test_cli_client_uses_explicit_ops_profile_and_logs_capability_change(self):
        import resume

        calls = []
        audit = Path(self.temp.name) / "capabilities.jsonl"

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if "list" in args:
                output = '[{"property_id":"device-offer","value":"ticket:1"}]'
            else:
                output = "{}"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        with patch("resume.subprocess.run", side_effect=run):
            client = resume.MulticaClient(profile="ops-fixture", audit_path=audit)
            client.set_offer("CHE-fixture", "device-offer", "ticket:1")
            self.assertEqual(client.read_offer("CHE-fixture", "device-offer"), "ticket:1")
        self.assertTrue(
            all(args[:3] == ["multica", "--profile", "ops-fixture"] for args, _ in calls)
        )
        event = json.loads(audit.read_text())
        self.assertEqual(event["kind"], "capability-change")
        self.assertIn("ICT", event["at"])
        self.assertNotIn("token", audit.read_text().lower())

    def test_rpc_and_cli_expose_park_claim_but_no_property_credentials(self):
        from daemon import main

        # Inspect CLI payloads without starting a device or a persistent daemon.
        with patch("daemon.rpc", return_value={"state": "queued"}) as rpc:
            self.assertEqual(
                main(
                    [
                        "park",
                        "--principal",
                        "agent",
                        "--agent-id",
                        "agent",
                        "--issue-id",
                        "CHE-wait",
                        "--ticket",
                        "ticket",
                        "--run-id",
                        "wait",
                    ]
                ),
                0,
            )
            self.assertEqual(rpc.call_args.args[1]["op"], "park")
        with patch("daemon.rpc", return_value={"state": "active"}) as rpc:
            self.assertEqual(
                main(
                    [
                        "claim",
                        "--principal",
                        "agent",
                        "--agent-id",
                        "agent",
                        "--issue-id",
                        "CHE-wait",
                        "--ticket",
                        "ticket",
                        "--run-id",
                        "successor",
                        "--pid",
                        str(os.getpid()),
                        "--idempotency-key",
                        "claim",
                    ]
                ),
                0,
            )
            payload = rpc.call_args.args[1]
            self.assertEqual(payload["args"]["binding"]["pid"], os.getpid())
        with BrokerServer(Path(self.temp.name) / "state", self.broker) as server:
            self.refused(
                "invalid_request",
                lambda: server.dispatch(
                    {
                        "op": "claim",
                        "args": {
                            "ticket_id": "t",
                            "principal": "agent",
                            "agent_id": "agent",
                            "issue_id": "i",
                            "run_id": "new",
                            "binding": {"mode": "remote"},
                            "idempotency_key": "k",
                        },
                    }
                ),
            )
            self.refused(
                "invalid_request", lambda: server.dispatch({"op": "start_offer_window", "args": {}})
            )

    def test_wait_cli_reports_multica_timeout_as_transport_failure(self):
        from daemon import main

        ticket = {"ticket_id": "t", "state": "queued", "offer_sequence": 0, "position": 1}
        with (
            patch("daemon.rpc", return_value=ticket),
            patch(
                "resume.MulticaClient.create_wakeup",
                side_effect=subprocess.TimeoutExpired("multica", 10),
            ),
        ):
            try:
                code = main(
                    [
                        "wait",
                        "--principal",
                        "agent",
                        "--agent-id",
                        "agent",
                        "--issue-id",
                        "CHE-wait",
                        "--ticket",
                        "t",
                        "--offer-property-id",
                        "p",
                    ]
                )
            except subprocess.SubprocessError:
                code = 99
        self.assertEqual(code, 2, "CLI must return its transport error code without a traceback")


if __name__ == "__main__":
    unittest.main()
