"""CHE-4515 contract checks. Synthetic phones; no device or Multica calls."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools/device-lease"))

from broker import Broker, Refused
from registry import Device, DeviceRegistry
from apps.admin_console.core.access_control import (
    AccessIdentity,
    AdminAPIError,
    admin_api_error_handler,
    public_tier,
)
from apps.admin_console.routers.leases import router
from apps.admin_console.services import device_leases as legacy

PREFIX = "/api/leases/dlp-1"


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 100_000.0
        self.calls = []
        claim_guard = threading.RLock()
        self.broker = Broker(
            Path(self.temp.name) / "broker.sqlite3",
            DeviceRegistry(
                [
                    Device("a", ("usb-a",), "rig", identity_confirmed=True, restricted=False),
                    Device("b", ("usb-b",), "rig", identity_confirmed=True, restricted=False),
                ]
            ),
            admitted={"a": "usb-a", "b": "usb-b"},
            clock=lambda: self.now,
            monotonic=lambda: self.now,
            admission_sources={"artemis": lambda: [], "android": lambda: []},
            admission_guard=lambda: claim_guard,
        )
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.add_exception_handler(AdminAPIError, admin_api_error_handler)
        self.identity = AccessIdentity(None, False, "cloudflare", service_principal="agent")
        self.app.dependency_overrides[public_tier] = lambda: self.identity
        self.app.state.device_lease_dispatch = self.dispatch
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        enabled = patch.dict("os.environ", ARTEMIS_DEVICE_LEASES="enabled")
        enabled.start()
        self.addCleanup(enabled.stop)

    def dispatch(self, op, args):
        self.calls.append((op, args.copy()))
        try:
            # The trusted owner verifies HTTP identity and binding before
            # dispatch; this fixture uses the live test process as its binding.
            args = {k: v for k, v in args.items() if k not in {"binding", "agent_id", "issue_id"}}
            return getattr(self.broker, op)(**args)
        except Refused as exc:
            result = {"code": exc.code, **exc.details}
            if ticket := result.get("ticket"):
                # The trusted HTTP adapter projects the broker's private queue
                # shape. Offered-ticket projection is covered by the queue mock.
                result["ticket"] = {
                    k: ticket[k] for k in ("ticket_id", "state", "agent_id", "issue_id", "position")
                }
                result["ticket"].update(
                    next_offer_sequence=ticket["offer_sequence"] + 1, offer=None
                )
            return result

    def body(self, **changes):
        body = dict(
            pool="rig",
            ttl=300,
            purpose="CHE-fixture",
            run_id="run-1",
            agent_id="fixture-agent",
            issue_id="CHE-fixture",
            binding=dict(kind="local", pid=123, process_start="42", boot_id="boot-fixture"),
            idempotency_key="acquire-1",
        )
        body.update(changes)
        return body

    def acquire(self, **changes):
        return self.client.post(PREFIX, json=self.body(**changes))

    def test_group_is_all_or_none_and_busy_holder_hides_handles(self):
        first = self.acquire(device_id="usb-a")
        self.assertEqual(first.status_code, 201, first.text)
        busy = self.acquire(count=2, run_id="run-2", idempotency_key="batch")
        self.assertEqual(busy.status_code, 409, busy.text)
        self.assertEqual(busy.json()["code"], "device_busy")
        self.assertEqual(busy.json()["holder"]["holder"], "agent")
        self.assertNotIn("lease_id", busy.json()["holder"])
        self.assertNotIn("run_id", busy.json()["holder"])
        with closing(sqlite3.connect(self.broker.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_devices").fetchone()[0], 1)
        # A later singleton cannot bypass the queued batch for the free device.
        later = self.acquire(device_id="b", run_id="run-3", idempotency_key="free")
        self.assertEqual(later.status_code, 409, later.text)
        self.assertEqual(
            first.json()["devices"][0],
            dict(device_id="a", endpoint="usb-a", restricted=False, space_epoch=None),
        )
        self.assertEqual(first.json()["started_at"], self.now)

    def test_admission_reader_failure_closes_http_grants(self):
        made = self.acquire(device_id="a")
        self.assertEqual(made.status_code, 201, made.text)

        def unavailable():
            raise OSError("fixture admission source unavailable")

        self.broker.admission_sources["android"] = unavailable
        denied = self.acquire(device_id="b", run_id="run-2", idempotency_key="unavailable")
        self.assertEqual(denied.status_code, 503, denied.text)
        self.assertEqual(denied.json()["code"], "dlp1_unavailable")
        with closing(sqlite3.connect(self.broker.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM broker_groups").fetchone()[0], 1)

    def test_acquire_and_renew_retries_do_not_extend_twice(self):
        made = self.acquire(count=2)
        self.assertEqual(made.status_code, 201, made.text)
        group = made.json()
        self.now += 20
        self.assertEqual(self.acquire(count=2).json(), group)
        self.now += 180
        body = dict(run_id="run-1", ttl=300, idempotency_key="renew-1")
        path = f"{PREFIX}/{group['lease_id']}/renew"
        renewed = self.client.post(path, json=body)
        self.assertEqual(renewed.status_code, 200, renewed.text)
        self.now += 20
        self.assertEqual(self.client.post(path, json=body).json(), renewed.json())
        body["ttl"] = 600
        self.assertEqual(self.client.post(path, json=body).json()["code"], "idempotency_conflict")

    def test_fencing_and_quarantine_remain_unavailable(self):
        made = self.acquire(count=2)
        self.assertEqual(made.status_code, 201, made.text)
        lease_id = made.json()["lease_id"]
        released = self.client.post(f"{PREFIX}/{lease_id}/release", json={"run_id": "run-1"})
        self.assertEqual(released.json()["state"], "fencing")
        for state in ("fencing", "quarantine"):
            if state == "quarantine":
                self.broker.recovery(
                    lease_id, "fencing", state, recovery_owner="ops", ops_issue="CHE-fixture"
                )
            refused = self.acquire(run_id="other", idempotency_key=state)
            self.assertEqual(refused.status_code, 409, refused.text)
            status = self.client.get(f"{PREFIX}/{lease_id}", params={"run_id": "run-1"})
            self.assertEqual(status.json()["state"], state)
            self.assertEqual(len(status.json()["devices"]), 2)

    def test_legacy_wire_contract_is_unchanged(self):
        lease = legacy.Lease("lease", "a", "agent", "qa", False, None, 1.0, 61.0)
        with (
            patch("apps.admin_console.routers.leases._candidates", return_value=["a"]),
            patch.object(legacy, "acquire", return_value=lease) as acquire,
        ):
            got = self.client.post("/api/leases", json={"device_id": "a", "ttl": 60})
        self.assertEqual(got.status_code, 201)
        self.assertEqual(
            got.json(),
            dict(
                lease_id="lease",
                device_id="a",
                holder="agent",
                purpose="qa",
                restricted=False,
                space_epoch=None,
                created_at=1.0,
                expires_at=61.0,
            ),
        )
        self.assertEqual(acquire.call_args.args[1:5], (["a"], "agent", 60, ""))
        self.assertEqual(self.calls, [])

    def test_queue_offer_park_and_successor_claim_contract(self):
        ticket = dict(
            ticket_id="ticket-1",
            state="queued",
            agent_id="fixture-agent",
            issue_id="CHE-fixture",
            position=2,
            next_offer_sequence=1,
            offer=None,
        )
        calls = []

        def dispatch(op, args):
            calls.append((op, args))
            if op == "acquire":
                return dict(
                    code="device_busy",
                    holder={"holder": "other", "expires_at": 101000},
                    ticket=ticket,
                )
            if op == "park":
                return dict(ticket, state="parked")
            if op == "ticket":
                return dict(
                    ticket,
                    state="offered",
                    offer=dict(
                        sequence=1,
                        signal="ticket-1:1",
                        signaled_at=100010,
                        expires_at=100910,
                        devices=["a", "b"],
                    ),
                )
            return self.dispatch(
                "acquire",
                dict(self.body(count=2, run_id=args["run_id"]), principal=args["principal"]),
            )

        self.app.state.device_lease_dispatch = dispatch
        queued = self.acquire(count=2)
        self.assertEqual(queued.status_code, 409, queued.text)
        self.assertEqual(queued.json()["ticket"], ticket)
        owner = dict(agent_id="fixture-agent", issue_id="CHE-fixture")
        parked = self.client.post(
            f"{PREFIX}/tickets/ticket-1/park", json=dict(owner, run_id="run-1")
        )
        self.assertEqual(parked.json()["state"], "parked")
        offered = self.client.get(f"{PREFIX}/tickets/ticket-1", params=owner)
        self.assertEqual(offered.json()["offer"]["signal"], "ticket-1:1")
        self.assertNotIn("endpoint", json.dumps(offered.json()))
        claim = self.body(run_id="run-2", idempotency_key="claim-1")
        for field in ("purpose", "pool", "ttl"):
            claim.pop(field)
        claimed = self.client.post(f"{PREFIX}/tickets/ticket-1/claim", json=claim)
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["run_id"], "run-2")
        self.assertEqual(calls[-1][1]["principal"], "agent")
        self.assertEqual(calls[-1][1]["binding"], claim["binding"])

    def test_extensions_fail_closed_without_backend_and_for_bad_input(self):
        del self.app.state.device_lease_dispatch
        response = self.acquire()
        self.assertEqual(response.status_code, 503, response.text)
        self.app.state.device_lease_dispatch = self.dispatch
        for changes in (
            {"principal": "victim"},
            {"ttl": True},
            {"ttl": 299},
            {"count": 0},
            {"binding": {"kind": "local", "pid": 1}},
            {"run_id": " "},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.acquire(**changes).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_anonymous_and_other_holders_cannot_use_extensions(self):
        self.identity = AccessIdentity(None, False, "cloudflare", "no_jwt")
        self.assertEqual(self.acquire().status_code, 401)
        self.assertEqual(self.calls, [])
        self.identity = AccessIdentity(None, False, "cloudflare", service_principal="agent")
        made = self.acquire()
        self.assertEqual(made.status_code, 201, made.text)
        self.identity = AccessIdentity(None, True, "cloudflare", service_principal="other")
        denied = self.client.post(
            f"{PREFIX}/{made.json()['lease_id']}/release", json={"run_id": "run-1"}
        )
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_invalid_backend_response_and_lost_reply_fail_closed(self):
        made = self.acquire()
        self.assertEqual(made.status_code, 201, made.text)
        result = made.json()
        # These failures must not return raw backend details or partial grants.
        for response in (
            [],
            {"code": "internal_error", "secret": "fixture-secret"},
            dict(result, expires_at=float("nan")),
        ):
            with self.subTest(response=response):
                self.app.state.device_lease_dispatch = lambda op, args: response
                got = self.acquire()
                self.assertEqual(got.status_code, 503, got.text)
                self.assertNotIn("fixture-secret", got.text)

        def lost_reply(op, args):
            self.dispatch(op, args)
            raise ConnectionError("fixture-secret")

        self.app.state.device_lease_dispatch = lost_reply
        failed = self.acquire(run_id="run-2", device_id="b", idempotency_key="lost")
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertNotIn("fixture-secret", failed.text)
        self.now += 20
        self.app.state.device_lease_dispatch = self.dispatch
        retry = self.acquire(run_id="run-2", device_id="b", idempotency_key="lost")
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["expires_at"], result["expires_at"])

    def test_extension_service_grants_do_not_inherit_legacy_grants(self):
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt
        from tests.unit.admin_console.test_device_leases_service import service_client

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
        public.update(kid="k", alg="RS256", use="sig")
        clients = service_client(
            (private, public),
            legacy=("agent", {("POST", "/api/leases")}),
            extension=("agent", {("POST", PREFIX), ("POST", PREFIX + "/{lease_id}/renew")}),
        )
        with clients("legacy") as client:
            self.assertEqual(client.post(PREFIX, json=self.body()).status_code, 403)
        with clients("extension") as client:
            client.app.state.device_lease_dispatch = self.dispatch
            made = client.post(PREFIX, json=self.body())
            self.assertEqual(made.status_code, 201, made.text)
            path = f"{PREFIX}/{made.json()['lease_id']}"
            self.now += 200
            renewed = client.post(
                path + "/renew", json=dict(run_id="run-1", ttl=300, idempotency_key="service-renew")
            )
            self.assertEqual(renewed.status_code, 200, renewed.text)
            self.assertEqual(
                client.post(path + "/release", json={"run_id": "run-1"}).status_code, 403
            )


if __name__ == "__main__":
    unittest.main()
