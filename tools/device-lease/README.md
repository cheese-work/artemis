# DLP-1 registry, broker, queue and recovery

The additive SmartQA HTTP contract is documented in
[SMARTQA-EXTENSIONS.md](SMARTQA-EXTENSIONS.md) (CHE-4515). Its backend stays
unconfigured in production; phase 1 installs no second allocating authority.

`registry.py` is a standard-library inventory and candidate filter for the
CHE-4285 rev 4 protocol. It performs no ADB, network, Multica or lease operation.
The existing SmartQA lease API keeps its CHE-2099 behavior. The broker core below
shares its transaction and canonical identity primitives.

`DeviceRegistry.load(path)` reads a trusted UTF-8 JSON array:

```json
[
  {
    "device_id": "rig-phone-1",
    "aliases": ["usb-fixture-1", "192.0.2.10:5555"],
    "pool": "rig",
    "model": "Fixture phone",
    "api": 34,
    "abi": "arm64-v8a",
    "restricted": false,
    "identity_confirmed": true
  }
]
```

This example is synthetic, not a live-device inventory. Canonical ids and aliases
come from a trusted identity source or verified inventory. Never infer identity
from a matching model, an IP address or a transport's appearance. Alias changes
require a new registry snapshot. Duplicate canonical ids and aliases shared by
different records reject the entire snapshot; insertion order never picks a winner.
Records are immutable. Malformed metadata raises `ValueError`; unreadable inventory
files raise `OSError`. Callers must leave admission closed on either error.

`resolve(alias)` returns the same `Device` for its canonical id, USB serial and
wireless aliases, or `None` for an unknown alias. Metadata includes `pool`, `model`,
`api`, `abi`, `restricted`, `identity_confirmed` and `restricted_policy`.
Unknown model, API and ABI values stay `None`; they are not guessed.

`eligible(alias, state, route="raw_adb")` requires a registered device, confirmed
identity and a fresh `device` state for that alias. Supply the state from trusted
discovery; inventory never caches readiness. Every other state, including
`unauthorized` and `no permissions`, is refused. New records default to restricted
and unconfirmed. This function does not check observation age: the caller must
expire stale observations before filtering candidates.

Restricted devices are refused for raw ADB. The only recognized restricted policy
is `restricted_policy="che-2129"`, with `route="che-2129"` and confirmed identity.
That result means the device is a candidate for the existing audited CHE-2129
command route; it never enables a shell, sync tunnel or root. Unknown policies
and unknown routes fail closed.

The default registry always contains `500c592` in pool `root`, restricted and
unconfirmed, with unknown model/API/ABI. It stays restricted if an inventory row
renames it or supplies `restricted=false`. There is no identity confirmation or
grant for that device in this change. CHE-2082/G5 still requires the device owner's
identity confirmation and per-device grant before any use. Pool membership grants
no permission, including root permission.

Eligibility is not admission. Consumers must still enforce device grants, Android
squad admission, testcase reviews, qualification, existing claims, Artemis execution
locks, lease ownership and leader-verified cleanup/release. The broker, recovery,
remote gateway and live-device recovery work are separate slices. No code here
starts or restarts an ADB server or changes a live device policy.

Run the hermetic tests from the repository root:

```sh
python3 tests/unit/runtime/test_device_registry.py -v
```

The same tests are discovered by the repository's normal pytest unit suite.

## Broker core

`broker.py` owns a SQLite lease-group domain. `device_lease` is the small shared
package used by this broker and the CHE-2099 service/router. It resolves canonical
ids without copying identity policy and samples the clock only after
`BEGIN IMMEDIATE`. Domain refusals commit expiry maintenance; interruptions and
unexpected exceptions roll back the complete grant, run budget and retry record.
The shared package is included in the existing wheel's package discovery.

The domain receives an immutable registry and a trusted `admitted` snapshot:
canonical device id -> currently allowed endpoint alias. The owner must collect
and validate readiness and grants before supplying that snapshot. Trusted
`admission_sources` readers additionally join current Artemis execution locks and
Android issue claims on every decision; aliases resolve to canonical devices.
Only snapshot entries with a matching registered endpoint and confirmed,
unrestricted identity can be allocated. No discovery, device command, network or
Multica call runs inside the writer transaction. The durable queue supplies renewal
conflicts. The optional `conflicting_waiter(conn, device_ids)` hook adds other pure
SQL conflicts. Clients cannot supply the snapshot or hook over RPC.

Acquire accepts `principal`, `run_id`, `purpose`, `pool`, optional `device_id`,
`count`, integer `ttl` and `idempotency_key`. Canonical selection, run uniqueness
and all-or-none reservation occur in one transaction. `count` defaults to 1.
An impossible batch raises `impossible_batch`; a busy batch raises `device_busy`
with a durable ticket and the holder whose expiry comes first. A head batch ticket
reserves the free remainder without granting access.
USB and wireless aliases never count as separate phones. A run cannot nest or
expand its lease group. Mixed pools are not supported by this slice.

A group response has `lease_id`, `holder`, `run_id`, `purpose`, `started_at`,
`expires_at`, `ttl`, `hard_deadline`, `state`, nullable recovery owner/ops issue and
`devices[]`. Every device has canonical `device_id`, `endpoint`, `restricted` and
nullable `space_epoch`. Timestamps are epoch seconds. Human displays must render
times separately in ICT. Lease ids identify rows; they are not credentials.

TTL defaults to 1800 s and accepts 300..3600 s. Pool `root` accepts at most 1800 s;
pool membership never grants root or device permission. Renewal requires at most
1/3 of the stored TTL remaining, an active unexpired lease, no drain and no eligible
conflicting waiter. The new expiry is `min(now + ttl, started_at + hard_cap)`.
The hard cap is 7200 s (3600 s for `root`). The first grant and deadline persist
per principal/run across release/reacquire; changing the key cannot reset them.

Successful acquire and renew results are stored with the request fingerprint in
the same transaction as the change. A retry with the same principal/run/key returns
the original response; a changed operation or arguments raises
`idempotency_conflict`. Refusals do not consume a key. An idempotent replay is a
historical response, never a new access grant: use `status` to read current state.
Checks at the eventual gateway must use current state, expiry and authority.

Release or observed expiry changes `active` to `fencing`; it never deletes
ownership. The trusted recovery worker can compare-and-set `fencing` -> `cleaning`
-> `verifying`, or move a recovery state to `quarantine` with a named recovery
owner and ops issue. These CAS calls record state after the trusted recovery worker
performs its actions. Every state except `released` blocks a new grant. There is no
client or automatic transition to `released`; only `verify_release` in the trusted
leader control plane can record that decision. Failed or uncertain recovery cannot
silently free a phone.

## Local daemon and clients

The executable `tools/device-lease/device-lease` uses only the Python standard
library. `serve` owns the database, a per-database exclusive file lock and a private
Unix socket. The default state directory is
`~/.local/state/artemis-device-lease` (0700); the socket is `broker.sock` (0600).
An existing state directory must already have the correct owner and permissions.
The lock file stays on disk after shutdown to preserve inode-based exclusion.
No client command opens or writes a database. `acquire`, `renew`, `release`,
`status`, `heartbeat`, `ticket-status` and `cancel-ticket` are allowed over
newline-delimited JSON RPC. Frames are bounded to
64 KiB and reads to 2 s; the cooperative transport handles one request at a time.

This CLI invocation illustrates the contract; the supplied daemon currently
admits no devices, even with an inventory. No live broker is installed or started
by this change. Live use requires ops to compose current grants/readiness, both
claim readers with their shared guard, and a deadline-bounded recovery adapter.
Remote authentication, isolation and single-authority cutover remain later slices.

```sh
tools/device-lease/device-lease acquire --pool rig --count 2 --ttl 1800 \
  --principal fixture-agent --run-id fixture-run --purpose CHE-fixture \
  --agent-id fixture-agent --issue-id CHE-fixture --pid "$AGENT_RUN_PID" \
  --idempotency-key fixture-acquire
tools/device-lease/device-lease renew --lease-id LEASE_ID --ttl 1800 \
  --principal fixture-agent --run-id fixture-run --idempotency-key fixture-renew
```

`--socket` before the subcommand selects a socket for a client; it never selects
an allocating authority. `serve --state-dir PATH [--registry FILE]` is the owner
entry point for an isolated development instance. Client exit codes: 0 for a
response, 1 for a domain refusal, 2 for configuration/transport failure.

`AGENT_RUN_PID` must name the live agent run process, not this short-lived CLI.
The CLI reads that PID's Linux start time and boot id and submits the complete
identity. RPC acquire refuses a missing binding or a remote binding. Direct
in-process `Broker.acquire` defaults to the current process identity; its
`agent_id` and `issue_id` default to `principal` and `purpose` for existing callers.
An explicitly empty identity field is invalid.

Same-UID local callers remain cooperative. Principal/run strings are labels,
not authentication. The private socket excludes other Unix users, but this slice
does not claim enforced isolation from agents sharing the daemon's Unix user.
Park, claim and Multica signaling are slice 3b. Recovery composition is described
below. Remote authentication
and access are slice 5. SmartQA extensions and single-authority cutover are slice 6;
the unchanged CHE-2099 service must not be enabled alongside a live broker as a
second admitting authority. `500c592` remains excluded from raw ADB (G5/CHE-2082).

Run the core and transport tests without dependencies or live devices:

```sh
python3 tests/unit/runtime/test_device_lease_broker.py -v
python3 tests/unit/runtime/test_device_lease_queue.py -v
```

## Durable queue and offers

Busy acquire commits one open ticket per issue in SQLite. A ticket records its
principal, agent, issue, pool/exact selector, count, TTL and FIFO position. It has
no run binding. Another acquire on the same issue returns that ticket only to
the same owner, including when the new run changes its request. Poll and cancel
require the principal, agent and issue to match. Cancellation frees its position.

Exact and pool selectors share one FIFO among conflicting eligible sets. A later
ticket or direct acquire can pass earlier tickets only when the sets are disjoint.
A head batch reserves free members until all N are free. Only then does it become
`offered`. Selection, run uniqueness, queue order and reservation share the writer
transaction. Offered selection stays stable while the devices remain eligible.
Offers provide no lease, credential or device endpoint access.

`start_offer_window(ticket_id, offer_sequence)` is a trusted worker hook, not an
RPC. Slice 3b calls it only after the matching Multica property write/read-back,
outside the writer transaction. Until then `offer_expires_at` is null. Repeating
the hook cannot extend a window. After 900 s without a claim, the ticket moves to
the tail; a second miss cancels it. Re-offering increments the sequence. Claim,
park, 75 min abandonment, wakeups and the property writer remain slice 3b work.

## Run liveness and restart barrier

Every active lease persists its run id and local PID/start-time/boot-id identity.
The broker reads local process identities before taking the writer lock, rejecting
dead, reused or zombie PIDs. Probe and clock seams permit hermetic tests. The
trusted domain also supports remote bindings and heartbeats every 30 s; 90 s
without a heartbeat fences the group. Remote authentication remains slice 5 and
remote acquisition is refused by this local transport. Heartbeats never extend
lease expiry or resurrect a fenced group.

The foreground daemon checks liveness, expiry and offers even when clients are
idle. Epoch expiries persist for responses and recovery. Monotonic deadlines
govern running lease/offer durations. A backward monotonic clock or a wall versus
monotonic discontinuity over 5 s fences active groups and invalidates offers.

After taking the exclusive database lock and before serving clients, the daemon
calls `restart()`. It fences every active group and persists a reconciliation
barrier, including unfinished recovery from earlier restarts. All new grants and
offers wait, even for disjoint devices. The trusted recovery worker must
bring each group to `verifying`, `quarantine` with its owner/ops issue, or verified
`released`, then call `finish_reconcile(lease_id)`. This acknowledgment cannot free
a device: `verifying` and `quarantine` still block reuse. No client can invoke
restart, reconciliation, signal acknowledgment or recovery over RPC. An embedded
owner must call `restart()` once after obtaining exclusive ownership too.

No daemon is installed or live admission enabled here. The owner-provided recovery
adapter must close streams, stop run-owned work and confirm cleanup. No device
command, ADB server reset or policy change is performed by the hermetic tests.

## Recovery and admission runbook (DLP-1 rules 5, 8–11, Authority 3)

1. **Compose before admitting.** Keep one exclusive broker owner and one authority.
   Pass trusted current `admitted` grants/readiness. Set `admission_sources` to
   `{"artemis": read_execution_locks, "android": read_issue_claims}`. Each reader
   returns a collection of canonical ids or registered aliases; include all existing
   claims, including the requesting run's. Read Artemis execution-lock metadata
   (`DeviceExecutionLock.get_active_owners`) on the intended host/endpoint, and
   Android issue claims through the authorized Multica client. Do not use
   `DevicePool.get_claimed_serials` here: that convenience reader swallows failures.
   Unknown identities, invalid snapshots and reader failures close new admission.
   Readers run outside SQLite's writer transaction. Supply `admission_guard`, a
   context-manager factory shared by both claim writers and broker decisions, to
   hold exclusion from source reads through commit. Without cooperative writers,
   the snapshot cannot prevent a new external claim racing the grant; do not
   enable live admission in that configuration. Missing either named source or
   the shared guard closes admission; inventory alone cannot bypass these checks.
2. **Record before changing.** The trusted owner calls
   `record_change(lease_id=..., principal=..., run_id=..., change=...)` before each
   allowed device mutation. A change names a canonical `device_id` in the lease.
   Supported typed intents are setting (`name`, `before`, `after`), app (`package`,
   `installed_by_run=true`, `allowed_remove=true`) and HOME (`allowed=true`).
   Each includes `kind` and `device_id`. The journal is append-only and cleanup
   walks it in reverse order. No arbitrary shell command is accepted. Only a
   trusted grant/execution adapter may assert app provenance and HOME permission;
   holder-supplied booleans are not authorization. Keep pre-existing apps/data.
3. **Fence and clean.** Configure `recovery_io`, `recovery_owner` and an existing
   `ops_issue`. `maintain()` is the foreground worker, also called by the daemon
   while idle. Release, expiry, revoke, suspect liveness and restart retain device
   ownership. `recovery_io.fence(group, timeout=remaining_seconds)` must reject
   new access, revoke credentials, close every stream and confirm termination of
   only old-run tracked work, including device processes and reverse forwards.
   Closing sockets alone is insufficient. Return exactly `True` only on confirmed
   success. `undo(group, change, timeout=...)` restores only recorded changes,
   compares current values to the recorded mutation, and must be idempotent.
   Both callbacks run outside the SQLite writer lock and honor their deadlines.
   The two-minute monotonic budget includes fencing and cleanup. Overdue recovery
   still attempts emergency fencing, then quarantines. Exceptions, false results
   and interrupted cleanup quarantine the whole group with the named owner/issue.
4. **Hold for independent release verification.** Successful cleanup stops at
   `verifying`. Preserve lease id, run binding, audit and cleanup evidence for the
   Android squad leader. Only the trusted leader control plane calls
   `verify_release(lease_id, verifier=LEADER_ID, evidence=REFERENCE, passed=True)`.
   `LEADER_ID` is `5cd75ce5-d323-4004-9779-7e2ccf16bde4` under the current policy.
   A failed verdict quarantines. The named recovery owner can supply evidence
   using `recover_quarantine`; this returns only to `verifying`, never directly
   to availability. A successful leader verdict records `released`. No TTL-only
   release exists. These control-plane hooks and `record_change` are absent from
   the holder socket/CLI. The caller authenticates the operator; a string id is
   not a credential, and same-UID access remains cooperative.
5. **Retain and inspect evidence.** Every committed lifecycle/ticket event is
   queued transactionally in `broker_audit`, then exported outside the writer
   transaction to `<database>.audit.jsonl` with sequence, ICT time, agent, run id,
   issue and canonical device id. A batch produces one line per member/event.
   Ticket events retain the originating run label, not a live run binding.
   Export appends and fsyncs; a retry validates the exact existing prefix before
   appending the missing suffix, without duplicating events after a lost response.
   Audit corruption or export failure closes new grants/renewals; fencing and
   cleanup continue, with events retained in SQLite. Ops restores the preserved
   exact log prefix and re-exports; never truncate/overwrite the audit in place.
   Keep an unrepaired device unavailable with its named owner and ops issue.

On startup, call `restart()` after taking the daemon lock; reconcile every held
group before new grants. A state already in `cleaning` is uncertain after an
interruption and requires manual recovery. Automatic verification, gateway
authentication, production wiring and cutover are outside this slice. Restricted
devices and `500c592` keep their existing G5/CHE-2082 restrictions. Neither broker
nor adapter may restart shared ADB or reset unrelated devices/data.

```sh
python3 tests/unit/runtime/test_device_lease_recovery.py -v
```

## Non-admitting SmartQA shadow (CHE-4517, phase 2)

Every core acquire, renew, release, status and recovery decision appends a
`broker_decisions` row in the same SQLite transaction as its outcome. The entry
contains the operation, request, ICT time, pre-decision inventory, admission map,
groups, run budgets, idempotency responses and the broker response or refusal.
Renewal also captures the existing SQL waiter's result. A rollback leaves no
decision; a lost response after commit leaves a replayable decision. Journal
failure rolls back the decision. Invalid RPC envelopes are transport errors,
not core decisions. The journal is private owner state, not a new client RPC.

Run the shadow explicitly in a separate process, after broker requests:

```sh
python3 tools/device-lease/shadow.py --database /private/state/leases.sqlite3 \
  --output /private/state/shadow.jsonl --after 0 --limit 100
```

Use the real private owner directory for these example paths. The command prints
the last processed sequence. Supply that number as `--after` on the next run.
The JSONL is append-only; retrying an older cursor may append duplicate sequences.
The bounded batch limit is 1..1000. No worker, timer or live backend is installed.

The reader opens SQLite with `mode=ro` and `query_only`, materializes its batch,
and closes the connection before evaluation. `smartqa_decide` computes the
proposed DLP-1 extension decision from immutable JSON bytes. It uses shared input
validation and registry eligibility, with independent selection and lifecycle
projection. It never calls the broker's allocating methods, the HTTP dispatch,
or the legacy CHE-2099 allocator. This is a projection of the extension contract,
not production SmartQA scheduler parity or queue/live-binding qualification.
Future queue operations need corresponding snapshot and projection support.

Each comparison records both inputs, both decisions, and `match`, `different`,
`error` or `timeout`. Only the generated top-level lease id is ignored when
comparing acquire results; device ids, timing, ownership and refusal details
remain significant. Evaluator exception text is excluded. A replacement pure
evaluator can be supplied as `compare(..., decide=client.decide)`; it receives
bytes, never a database connection or an allocating client capability.

Shadow exceptions or timeouts cannot change broker state or hold its transaction.
A hung evaluator stalls only the separately invoked shadow process; the caller
owns that process's deadline. Comparison output errors also affect only that
invocation. Output files are private (0600), regular, and cannot alias the source
database or its sidecars. Keep logs inside the private owner directory: snapshots
contain device endpoints and principal/run labels. Full snapshots retain history;
measure journal size before any live rollout and define owner-controlled retention
there. This slice does not enable live admission or approve G4 cutover.

```sh
TMPDIR=/tmp python3 tests/unit/runtime/test_device_lease_shadow.py -v
```
