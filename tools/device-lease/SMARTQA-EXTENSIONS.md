# CHE-2099 DLP-1 API extensions (CHE-4515)

These are additive extensions to CHE-2099, implementing the Contract and SmartQA
migration sections of CHE-4285 rev 4. The existing `/api/leases` request, response,
TTL range, authentication, busy response and legacy lifecycle remain unchanged.
Existing clients do not opt in implicitly. The extensions live under
`/api/leases/dlp-1` and appear in the application's OpenAPI document.

## Phase 1 boundary

The local broker is the only DLP-1 authority. SmartQA does not allocate in phase 1.
This slice publishes the HTTP contract and adapter, not an enabled allocator.
No production startup code installs the adapter's backend. With the existing
`ARTEMIS_DEVICE_LEASES` flag off, the extension returns 404. With the flag on but
no backend installed, it returns 503 `dlp1_unavailable`. There is no fallback to
the CHE-2099 allocator and no new database, broker daemon, device command or
Multica client in the HTTP adapter.

The trusted authority owner can supply the synchronous callable
`app.state.device_lease_dispatch(operation, arguments)` at application startup.
It must return a group, a ticket, or a refusal dictionary (`code` plus documented
details). Tests inject this callable with synthetic devices. Installing a live
backend is not part of this slice: it requires a single host-wide authority,
the migration/cutover work and Cheese's G4 approval. Never enable the legacy
allocator and broker independently against the same physical devices.

The adapter does not implement scheduling, validate process liveness, issue
credentials, or authenticate remote heartbeats. Those remain broker duties.
The queue/live-binding slice supplies ticket, park and claim behavior; its
implementation is not copied into SmartQA. A backend must verify authenticated
principal, agent/issue ownership, live run binding and current authority before
any admission. Body strings and a structurally valid binding are not evidence
of liveness or a device grant. Requests never select an allocating authority.

## Routes and requests

All routes require an authenticated service principal and its explicit route
grant through the existing middleware. Principal is derived from authentication,
never from JSON. Human/admin access to the legacy routes is unchanged; admin
privileges do not bypass DLP-1 run ownership. Unknown body fields are rejected.
Strings must be nonempty (surrounding whitespace is trimmed); TTL and count are
strict integers. For service grants, include the exact method and route below;
template segments match one nonempty path segment only.

| Method and suffix under `/api/leases/dlp-1` | Operation | Input | Success |
| --- | --- | --- | --- |
| `POST ""` | `acquire` | Acquire body | 201 group |
| `GET /{lease_id}` | `status` | `run_id` query | 200 group |
| `POST /{lease_id}/renew` | `renew` | `run_id`, `ttl`, `idempotency_key` | 200 group |
| `POST /{lease_id}/release` | `release` | `run_id` | 200 group (fencing) |
| `GET /tickets/{ticket_id}` | `ticket` | `agent_id`, `issue_id` query | 200 ticket |
| `POST /tickets/{ticket_id}/park` | `park` | `agent_id`, `issue_id`, `run_id` | 200 ticket |
| `POST /tickets/{ticket_id}/claim` | `claim` | `agent_id`, `issue_id`, `run_id`, `binding`, `idempotency_key` | 200 group |

An acquire body (all values below are synthetic):

```json
{
  "pool": "rig",
  "count": 2,
  "ttl": 1800,
  "purpose": "CHE-fixture",
  "agent_id": "fixture-agent",
  "issue_id": "CHE-fixture",
  "run_id": "fixture-run",
  "binding": {
    "kind": "local",
    "pid": 123,
    "process_start": "42",
    "boot_id": "fixture-boot"
  },
  "idempotency_key": "fixture-acquire-1"
}
```

`pool` defaults to `rig`, `count` to 1, `ttl` to 1800. Optional `device_id`
narrows the pool to that canonical id or registered alias. `count` is all or
none, never partial. Mixed needs, nested acquire and later expansion are not
supported by this broker core. USB/wireless aliases name one physical device.
A remote binding uses `{"kind":"remote","heartbeat_id":"fixture-heartbeat"}`;
the owner must resolve this opaque reference to an authenticated first heartbeat.
It never accepts a client-declared principal or timestamp as that proof.

TTL is integer seconds, 300..3600; the broker further limits `root` to 1800.
The broker applies final-third renewal, conflicting-waiter refusal and the
original run's hard cap. A renewal sets `expires_at = min(now + ttl,
hard_deadline)`. Acquire, renew and claim keys bind to their request fingerprint
and authenticated run. Reuse the same key after a lost response; changed
arguments produce 409 `idempotency_conflict`. A replay is a historical response,
not renewed access. Read current status before use; the gateway must check
current state, expiry and authority on every access.

## Responses

A group contains `lease_id`, `holder`, `run_id`, `purpose`, `started_at`,
`expires_at`, `ttl`, `hard_deadline`, `state`, nullable `recovery_owner` and
`ops_issue`, plus a nonempty `devices[]` list. Each device has canonical
`device_id`, its own `endpoint`, `restricted` and nullable `space_epoch`.
Timestamps are epoch seconds. User-facing displays render times separately in
ICT. A lease id identifies a row; it is not a credential.

The recovery states `fencing`, `cleaning`, `verifying` and `quarantine` preserve
ownership and make the whole group unavailable. `released` requires verified
recovery, including the Android leader's release verification in phase 1.
There is no holder recovery/revoke/force-release HTTP operation. Neither TTL
expiry nor a release response proves cleanup, isolation or permission to regrant.

A busy response is HTTP 409 with `code: "device_busy"`, a public `holder`
summary and, when supplied by the queue backend, `ticket`. The rival's lease id,
run binding and endpoints are excluded. A ticket contains `ticket_id`, `state`
(`queued`, `parked`, `offered`, `claimed`, `cancelled`), `agent_id`, `issue_id`,
nullable `position`, `next_offer_sequence` and nullable `offer`.

An offer contains `sequence`, `signal` (`<ticket id>:<sequence>`), canonical
`devices[]`, nullable `signaled_at` and `expires_at`. The broker sets the times
only after its Multica property write is read back. Offers reserve devices;
they convey no endpoint, credential or ADB access. Park drops the old run's
binding. Claim requires a live successor run of the same authenticated
principal, agent and issue. Expired/missed offers and cancellation are decided
by the queue backend, not the adapter. Poll/park/claim never create Multica
wakeups in this process; the parking client follows DLP-1 Waiting and resume.

Refusals preserve the broker's code: ownership errors are 403, unknown lease,
ticket or device is 404, expired lease/ticket is 410, malformed requests are
422, and scheduling/idempotency/state conflicts are 409. Missing/unavailable
backend, transport failure or invalid backend response is 503
`dlp1_unavailable`; raw exception details are not returned. Success responses
are schema-filtered so backend-only fields do not become public API fields.

## Verification and next phases

Run from the repository root, using the repository's Python environment:

```sh
python tests/unit/runtime/test_lease_extensions.py -v
python tests/unit/runtime/test_device_lease_broker.py -v
```

The first suite exercises the real core through HTTP for batch atomicity,
acquire/renew retries and blocked recovery states. The queue exchange uses a
fake backend: it verifies forwarding and serialization, not queue scheduling,
process liveness, remote authentication or real Multica signaling. No live
device command runs. Existing CHE-2099 suites remain compatibility checks.

Phase 2 (slice 6b) compares decisions from a read-only snapshot and never calls
SmartQA allocate/renew/release. Phase 3 (slice 6c then G4) drains leases, carries
quarantine, fences the old generation and only then enables SmartQA with service
authentication. This API contract neither performs nor approves that cutover.
Restricted-device grants, `500c592` identity confirmation (CHE-2082/G5), no
agent ADB server restart and leader-verified release all remain required.
