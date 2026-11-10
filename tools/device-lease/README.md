# DLP-1 registry and broker core (CHE-4506, CHE-4508)

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
and validate readiness, grants, claims and execution locks before supplying that
snapshot. Only snapshot entries with a matching registered endpoint and confirmed,
unrestricted identity can be allocated. No discovery, device command, network or
Multica call runs inside the writer transaction. The `conflicting_waiter(conn,
device_ids)` hook must use only the current SQLite transaction; slice 3 supplies
the eligible conflicting ticket query. The default is empty because no queue
exists in this slice. Clients cannot supply the snapshot or hook over RPC.

Acquire accepts `principal`, `run_id`, `purpose`, `pool`, optional `device_id`,
`count`, integer `ttl` and `idempotency_key`. Canonical selection, run uniqueness
and all-or-none reservation occur in one transaction. `count` defaults to 1.
An impossible batch raises `impossible_batch`; a busy batch raises `device_busy`
with the holder whose expiry comes first, without reserving the free remainder.
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
owner and ops issue. These calls only record state. They perform no fencing or
cleanup action. Every state except `released` blocks a new grant. There is no
client or automatic transition to `released`: slice 4 must implement leader-verified
release. Failed or uncertain recovery cannot silently free a phone.

## Local daemon and clients

The executable `tools/device-lease/device-lease` uses only the Python standard
library. `serve` owns the database, a per-database exclusive file lock and a private
Unix socket. The default state directory is
`~/.local/state/artemis-device-lease` (0700); the socket is `broker.sock` (0600).
An existing state directory must already have the correct owner and permissions.
The lock file stays on disk after shutdown to preserve inode-based exclusion.
No client command opens or writes a database. Only `acquire`, `renew`, `release`
and `status` are allowed over newline-delimited JSON RPC. Frames are bounded to
64 KiB and reads to 2 s; the cooperative transport handles one request at a time.

This CLI invocation illustrates the contract; the supplied daemon currently
admits no devices, even with an inventory. No live broker is installed or started
by this change. Do not use the core as the live authority until the later slices
connect admission and recovery.

```sh
tools/device-lease/device-lease acquire --pool rig --count 2 --ttl 1800 \
  --principal fixture-agent --run-id fixture-run --purpose CHE-fixture \
  --idempotency-key fixture-acquire
tools/device-lease/device-lease renew --lease-id LEASE_ID --ttl 1800 \
  --principal fixture-agent --run-id fixture-run --idempotency-key fixture-renew
```

`--socket` before the subcommand selects a socket for a client; it never selects
an allocating authority. `serve --state-dir PATH [--registry FILE]` is the owner
entry point for an isolated development instance. Client exit codes: 0 for a
response, 1 for a domain refusal, 2 for configuration/transport failure.

Same-UID local callers remain cooperative. Principal/run strings are labels,
not authentication. The private socket excludes other Unix users, but this slice
does not claim enforced isolation from agents sharing the daemon's Unix user.
Live run bindings, heartbeat, restart/clock-jump reconciliation and queue tickets
are slice 3. Fencing actions and live admission are slice 4. Remote authentication
and access are slice 5. SmartQA extensions and single-authority cutover are slice 6;
the unchanged CHE-2099 service must not be enabled alongside a live broker as a
second admitting authority. `500c592` remains excluded from raw ADB (G5/CHE-2082).

Run the core and transport tests without dependencies or live devices:

```sh
python3 tests/unit/runtime/test_device_lease_broker.py -v
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
