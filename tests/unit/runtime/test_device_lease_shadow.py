"""Phase 2 uses synthetic inventory and offline snapshots, never a device API."""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))
from broker import Broker, Device, DeviceRegistry, Refused


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "broker.db"
        self.now = 100_000.0
        self.broker = Broker(
            self.db,
            DeviceRegistry([Device("a", ("usb-a",), restricted=False, identity_confirmed=True)]),
            admitted={"a": "usb-a"},
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            admission_sources={"artemis": lambda: [], "android": lambda: []},
            admission_guard=threading.RLock,
        )

    def take(self, **changes):
        args = dict(
            principal="agent",
            run_id="run",
            purpose="CHE-fixture",
            ttl=300,
            idempotency_key="acquire",
        )
        args.update(changes)
        return self.broker.acquire(**args)

    def records(self):
        with sqlite3.connect(self.db) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='broker_decisions'"
            ).fetchone():
                return []
            return [
                json.loads(row[0])
                for row in conn.execute("SELECT entry FROM broker_decisions ORDER BY sequence")
            ]

    def test_journal_captures_predecision_inputs_success_refusal_and_replay(self):
        group = self.take()
        self.assertEqual(self.take(), group)
        with self.assertRaises(Refused):
            self.take(run_id="rival", idempotency_key="busy")
        with self.assertRaises(Refused):
            self.take(ttl=1)
        records = self.records()
        self.assertEqual(len(records), 4)
        first = records[0]
        self.assertEqual(first["operation"], "acquire")
        self.assertEqual(first["request"]["principal"], "agent")
        self.assertEqual(first["snapshot"]["groups"], [])
        self.assertEqual(first["snapshot"]["admitted"], {"a": "usb-a"})
        self.assertEqual(first["broker"], group)
        self.assertTrue(first["time_ict"].endswith(" ICT"))
        self.assertEqual(len(records[1]["snapshot"]["groups"]), 1)
        self.assertEqual(records[1]["broker"], group)
        self.assertEqual(records[2]["broker"]["code"], "device_busy")
        self.assertEqual(records[3]["broker"]["code"], "invalid_request")

    def test_rollback_has_no_decision_but_lost_response_keeps_committed_decision(self):
        def fail(name):
            if name == "acquire:device":
                raise RuntimeError("interrupted transaction")

        self.broker.boundary = fail
        with self.assertRaises(RuntimeError):
            self.take()
        self.assertEqual(self.records(), [])

        def lose_response(name):
            if name == "acquire:committed":
                raise ConnectionError("response lost")

        self.broker.boundary = lose_response
        with self.assertRaises(ConnectionError):
            self.take()
        self.assertEqual(len(self.records()), 1)

    def test_shadow_uses_readonly_bytes_and_never_allocates_renews_or_releases(self):
        from shadow import compare, smartqa_decide

        group = self.take()
        self.now += 200
        self.broker.renew(
            lease_id=group["lease_id"],
            principal="agent",
            run_id="run",
            ttl=300,
            idempotency_key="renew",
        )
        self.broker.release(lease_id=group["lease_id"], principal="agent", run_id="run")
        before = self.db.read_bytes()
        client = Mock()
        client.decide.side_effect = smartqa_decide
        output = Path(self.temp.name) / "comparison.jsonl"
        self.assertEqual(compare(self.db, output, decide=client.decide), 3)
        self.assertEqual(client.decide.call_count, 3)
        self.assertTrue(all(isinstance(c.args[0], bytes) for c in client.decide.call_args_list))
        client.allocate.assert_not_called()
        client.renew.assert_not_called()
        client.release.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual([r["comparison"] for r in self.comparisons(output)], ["match"] * 3)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    @staticmethod
    def comparisons(path):
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_divergence_contains_both_inputs_and_both_decisions(self):
        from shadow import compare

        self.take()
        output = Path(self.temp.name) / "comparison.jsonl"
        compare(self.db, output, decide=lambda value: {"code": "device_busy"})
        rows = self.comparisons(output)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["comparison"], "different")
        self.assertEqual(row["broker_input"], row["shadow_input"])
        self.assertEqual(row["broker_input"]["request"]["principal"], "agent")
        self.assertEqual(row["broker"]["state"], "active")
        self.assertEqual(row["shadow"], {"code": "device_busy"})

    def test_stalled_or_failed_shadow_does_not_hold_broker_transaction(self):
        from shadow import compare

        self.take()
        entered, finish = threading.Event(), threading.Event()
        errors = []
        output = Path(self.temp.name) / "comparison.jsonl"

        def stalled(value):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test failed to release fake")
            raise TimeoutError("private client details")

        def run():
            try:
                compare(self.db, output, decide=stalled)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(entered.wait(2), "shadow never consumed the committed decision")
            # Busy is a committed broker refusal while the shadow is still blocked.
            with self.assertRaises(Refused) as caught:
                self.take(run_id="rival", idempotency_key="busy")
            self.assertEqual(caught.exception.code, "device_busy")
            self.assertFalse(finish.is_set())
            self.assertTrue(worker.is_alive(), "broker waited for the stalled evaluator")
        finally:
            finish.set()
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.comparisons(output)[0]["comparison"], "timeout")
        self.assertNotIn("private client details", output.read_text())

        def failure(value):
            raise RuntimeError("private exception")

        compare(self.db, output, decide=failure, after=1)
        self.assertEqual(self.comparisons(output)[-1]["comparison"], "error")
        self.assertEqual(len(self.records()), 2)

    def test_snapshot_prediction_covers_replay_busy_expiry_and_recovery(self):
        from shadow import compare

        group = self.take()
        self.take()
        with self.assertRaises(Refused):
            self.take(run_id="rival", idempotency_key="busy")
        self.now += 300
        self.broker.status(lease_id=group["lease_id"], principal="agent", run_id="run")
        self.broker.recovery(
            group["lease_id"],
            "fencing",
            "quarantine",
            recovery_owner="ops",
            ops_issue="CHE-fixture",
        )
        with self.assertRaises(Refused):
            self.take(run_id="rival", idempotency_key="quarantine")
        output = Path(self.temp.name) / "comparison.jsonl"
        self.assertEqual(compare(self.db, output, limit=2), 2)
        self.assertEqual(compare(self.db, output, after=2), 6)
        rows = self.comparisons(output)
        self.assertEqual([r["comparison"] for r in rows], ["match"] * 6)
        self.assertEqual(compare(self.db, output, after=6), 6)
        self.assertEqual(len(self.comparisons(output)), 6)

    def test_output_cannot_overwrite_the_database_or_follow_a_symlink(self):
        from shadow import compare

        self.take()
        before = self.db.read_bytes()
        with self.assertRaises(ValueError):
            compare(self.db, self.db)
        link = Path(self.temp.name) / "output-link"
        link.symlink_to(self.db)
        with self.assertRaises((ValueError, OSError)):
            compare(self.db, link)
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
