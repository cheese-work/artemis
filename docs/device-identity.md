# Device identity

Contract for CHE-1332. One physical phone is one device with one owner, one
lane and one execution reservation, however many ways it is connected. Routes
and error bodies live in [board API](board-api.md); who may act lives in
[board permissions](board-permissions.md).

## Model

- **Device:** a durable record with an opaque public `device_id`. It survives
  disconnects; a disconnected device stays in inventory.
- **Connection:** a child row per way the device is reached: `usb`, `wifi`,
  `bridge` (browser WebUSB) or `avd`, from source `local`, `bridge` or `host`.
  The `{source, host_id, serial}` tuple is the connection's routing key and
  never a public device id.
- **Alias:** a `device_aliases` row that keeps a merged-away `device_id`
  resolvable.
- **Reservation:** execution exclusivity is keyed by `device_id` for every
  execution ingress (`POST /api/run`, `POST /api/queue`, SDK and MCP). It is
  separate from the run's immutable transport binding. Only a device that is
  not yet resolved locks by its connection key.
- **Run snapshot:** each run records the `connection_id` it ran on, so Split
  re-attributes history deterministically.

## Matching rules

The reconciler matches every connection it sees to a device by a hardware
identity.

| Source | Hardware identity |
| --- | --- |
| Host agent | The agent reads `ro.serialno` and sends `HMAC-SHA256(salt, ro.serialno)`. The server issues the salt at enrolment and keeps it server-side; the raw serial never leaves the host. |
| Server adb (`local`), USB or Wi-Fi | The server reads `ro.serialno` over adb and computes the same HMAC. The transport serial (`ip:port` for Wi-Fi) is never the identity. |
| Browser bridge | The server reads `ro.serialno` through the bridge and computes the same HMAC. The `127.0.0.1:<port>` serial is never the identity. |
| Emulator (AVD) | `(host_id, AVD name)`. The AVD is the device; each booted instance is a connection. A recycled AVD with the same name stays the same device. |
| Old host agent (no hashed id) | No hardware identity. Matched to the existing connection with the same `(host_id, opaque serial)`; if none exists, an **uncertain match** is offered. It never creates a new device per reconnect. |

### Match outcomes

| Outcome | When | Schedulable |
| --- | --- | --- |
| `confirmed` | The hardware identity equals an existing device's, or is new (a new device is created). | Yes |
| `uncertain` | No hardware identity, and a likely existing device (same host, same model, or an old agent). The pair is shown as "May be the same phone as …". | No. Both records share one reservation until the owner decides, so the phone is never double-booked. |
| `provisional` | adb reports `unauthorized` or `offline`, or a Wi-Fi device is not paired yet, so `ro.serialno` cannot be read. | No. The record becomes `confirmed` once the identity is read. |

A caller who queues to a device that is not `confirmed` gets the fan-out status
`identity_unresolved` or the error `device_identity_unresolved`.

## Merge, Keep separate and Split

| Action | Who | Effect |
| --- | --- | --- |
| Merge | The device owner | The uncertain record joins the surviving device: its connections move, and its `device_id` becomes an alias. |
| Keep separate | The device owner | Records a non-match for this pair of connections; the pair is not offered again. Both records become `confirmed`. |
| Split | The device owner | Moves one connection out to a new device record. Runs whose snapshot `connection_id` is that connection move with it. |

Rules:

- Merge across two different owners is blocked (`device_identity_unresolved`).
  An administrator reassigns ownership first.
- Merge never transfers access: runs and notes keep their own owners and the
  [permission matrix](board-permissions.md#permission-matrix) is unchanged.
- Merge and Split are serialized against queue admission. Either is refused
  while affected work is running or queued (`device_claimed`).
- Merge and Split never rewrite history rows. Both are recorded in
  `device_events`.
- A non-owner sees "Waiting for <Owner> to confirm this device" and gets
  `device_not_yours` 403 on the action routes.

## Aliases

A merged-away `device_id` keeps working in URLs, filters (`device_id=`) and API
calls. The answer carries the surviving device and its `canonical_device_id`.
Clients replace the stored id with `canonical_device_id`. A Split never reuses
an alias; the split-out device gets a new `device_id`.

## Device state

One reconciler owns device state for every source:

- Heartbeats use the server's receive time, not the sender's clock.
- A connection with no heartbeat for `ARTEMIS_DEVICE_TTL_S` is `unknown`,
  never `idle`.
- Hysteresis: a new state must hold for 5 seconds before an event is written,
  so Wi-Fi flapping writes a bounded number of events.
- Transitions are idempotent `device_events` rows, indexed by
  `(device_id, ts)`, with retention and compaction.
- On startup the reconciler writes an `unknown` interval from the last known
  state to now. The availability timeline never claims a device was idle while
  nobody knew.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| One phone shows two lanes | USB and Wi-Fi connected before either read `ro.serialno`, or an old host agent | Wait for the identity read; otherwise the owner chooses Merge. Update the host agent. |
| "May be the same phone as …" keeps coming back | Old host agent sends no hashed id | Update the host agent, or choose Keep separate once. |
| Device stuck as provisional | USB debugging not authorized, adb `offline`, or Wi-Fi not paired | Accept the RSA prompt on the phone, replug, or finish `adb pair`. |
| Wrong runs on a device after Merge | Two phones merged by mistake | The owner chooses Split on the wrong connection; its runs move with it. |
| Merge refused with `device_identity_unresolved` | The two records have different owners | An administrator reassigns ownership, then the owner merges. |
| Merge or Split refused with `device_claimed` | Work is running or queued on the device | Let the run finish or cancel the queued items, then retry. |
| Lane shows Unknown | No heartbeat for `ARTEMIS_DEVICE_TTL_S`, or the host is unreachable | Check the host agent and its network; the lane recovers on the next heartbeat. |
| Old link with a merged-away id | Normal after Merge | The link still works; the page shows the surviving device. |
