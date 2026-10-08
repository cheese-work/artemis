# Board API, live updates and error catalog

Contract for CHE-1332 (device board, fan-out queue, run search, notes). This
file is the single definition of the routes, the fan-out statuses, the SSE
events and the error codes. Scopes and the permission matrix live in
[board permissions](board-permissions.md); device matching lives in
[device identity](device-identity.md); config keys and migrations live in
[board operations](board-operations.md).

Routes keep `session_id` as the run id. There is no `run_id` alias. The public
device id is the opaque `device_id` (for example `dev_01J9Z3K4M5`); the
`{source, host_id, serial}` routing tuple appears only inside `connections[]`.

## Calling the API

```bash
ARTEMIS=https://artemis.example.com
# Cloudflare Access (normal mode)
AUTH="Cf-Access-Jwt-Assertion: $CF_ACCESS_TOKEN"
# Opted-in synthetic preview only (docs/preview-fixtures.md): qa-a, qa-b or admin
AUTH="X-Artemis-Preview-Identity: qa-a"
```

Identity headers are trusted only behind the authenticating proxy; see
[board permissions](board-permissions.md#caller-classes).

## Routes

| Method and path | Purpose |
| --- | --- |
| `POST /api/queue` | Fan one goal out to several devices. |
| `GET /api/board` | Board snapshot: lanes, queue and recent runs per device. |
| `PUT /api/board/lanes/{device_id}/preference` | Per-user lane pin and hide. |
| `GET /api/inventory/devices` | Canonical device list. Replaces `/api/devices`. |
| `GET /api/inventory/devices/{device_id}` | Device detail: owner, connections, identity state. |
| `GET /api/inventory/devices/{device_id}/timeline` | Availability timeline. |
| `POST /api/inventory/devices/{device_id}/merge` | Merge an uncertain match. |
| `POST /api/inventory/devices/{device_id}/keep-separate` | Reject an uncertain match. |
| `POST /api/inventory/devices/{device_id}/split` | Undo a wrong merge. |
| `GET /api/runs` | Existing run list, extended with the search filters below. |
| `GET, POST /api/runs/{session_id}/annotations` | List or add annotations. |
| `PATCH, DELETE /api/runs/{session_id}/annotations/{annotation_id}` | Edit, resolve or delete an annotation. |
| `GET /api/runs/{session_id}/annotations/{annotation_id}/evidence` | Resolve an annotation anchor. |
| `GET, POST /api/runs/{session_id}/comments` | List or add comments. |
| `PATCH, DELETE /api/runs/{session_id}/comments/{comment_id}` | Edit or delete a comment. |
| `GET /api/stream` | Existing SSE stream; carries the board events below. |

Every route is classified in the access-control and preview route registries
([authorization closure](board-permissions.md#authorization-closure)).

### `POST /api/queue`

Request: exactly one of `device_ids` or `target`, plus the goal and the same
run options as `POST /api/run` (`profile`, `expected_output`, and so on;
`device_serial`, `device_ref`, `bridge_session_id`, `session_id` and `images`
are not accepted).

```bash
curl -sS -X POST "$ARTEMIS/api/queue" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "device_ids": ["dev_01J9Z3K4M5", "dev_01J9Z3K4M6", "dev_01J9Z3K4M7"],
  "goal": "Open Settings and turn on Dark theme",
  "client_submission_id": "qa-a-2026-10-08-0001"
}'

curl -sS -X POST "$ARTEMIS/api/queue" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "target": "all_my_idle",
  "goal": "Log in with the demo account",
  "client_submission_id": "qa-a-2026-10-08-0002"
}'
```

Response, always `200` when the request is valid, one result per target:

```json
{
  "client_submission_id": "qa-a-2026-10-08-0001",
  "replayed": false,
  "results": [
    {"device_id": "dev_01J9Z3K4M5", "status": "queued", "session_id": "20261008_101502_a1b2c3d4", "position": 1},
    {"device_id": "dev_01J9Z3K4M6", "status": "queued_behind", "session_id": "20261008_101502_e5f6a7b8", "position": 3},
    {"device_id": "dev_01J9Z3K4M7", "status": "offline_not_queued"}
  ],
  "summary": {"queued": 1, "queued_behind": 1, "offline_not_queued": 1}
}
```

#### Fan-out statuses

| Status | Run created | Meaning |
| --- | --- | --- |
| `queued` | Yes | Next on this device. |
| `queued_behind` | Yes | Behind a running run or other queued work on this device. |
| `queued_global_limit` | Yes | The device is free but the server-wide run limit is reached. |
| `offline_not_queued` | No | The device has no live connection. |
| `claimed` | No | The device stopped being available to the caller between selection and submit (another owner holds it, or it was unshared). |
| `identity_unresolved` | No | The device is in an uncertain or provisional identity state ([device identity](device-identity.md#match-outcomes)). |
| `admission_closed` | No | The server is draining (`/api/system/drain`). |

Created runs carry `session_id` and `position` (1 = next to start on that
device). `target: "all_my_idle"` resolves to the caller's own devices that are
`idle` at acceptance; it never includes shared devices.

#### Retries and the submission ledger

- `client_submission_id` is required: 1 to 128 characters from
  `[A-Za-z0-9._-]`, unique per caller.
- The ledger stores the caller, `client_submission_id`, a fingerprint of the
  request (canonical JSON without `client_submission_id`), the frozen target
  set, each target's `session_id` and its status. `all_my_idle` is frozen at
  first acceptance.
- A repeat with the same caller, key and fingerprint returns the original
  `results` with `"replayed": true`. It never creates, re-targets or re-orders
  work, also after a partial accept and a restart.
- A repeat with the same key and a different fingerprint answers
  [`submission_conflict`](#submission_conflict) 409.
- Ledger entries are kept 7 days.
- `POST /api/run` keeps its existing client-chosen `session_id` retry contract.

### `GET /api/board`

| Parameter | Default | Notes |
| --- | --- | --- |
| `my_devices`, `my_runs`, `scope` | `true`, `true`, unset | [Board toggles](board-permissions.md#board-toggles) |
| `device_id` | all lanes | Repeatable. |
| `limit` | `ARTEMIS_BOARD_PREVIEW_LIMIT` | 1 to 20; caps `queue.items` and `recent.items`. |

```bash
curl -sS "$ARTEMIS/api/board?my_devices=false&my_runs=true&limit=3" -H "$AUTH"
```

```json
{
  "watermark": {"boot_id": "b7f3c2e1", "version": 1842},
  "generated_at": "2026-10-08T03:15:02Z",
  "lane_count": 2,
  "attention_count": 1,
  "lanes": [
    {
      "device": {
        "device_id": "dev_01J9Z3K4M5",
        "label": "Pixel 6 Pro",
        "model": "Pixel 6 Pro",
        "owner_id": "qa-a@example.com",
        "identity": "confirmed",
        "active_connection": {"connection_id": "con_01J9Z3P1", "kind": "usb", "host_id": "host_01J8X2", "serial": "1A2B3C4D5E6F"},
        "connection_count": 2
      },
      "state": "running",
      "last_seen_at": "2026-10-08T03:15:01Z",
      "attention": [
        {"reason": "review_pending", "session_id": "20261008_091200_c0ffee00", "actions": ["accept", "reject"]}
      ],
      "running": {"session_id": "20261008_101502_a1b2c3d4", "goal": "Open Settings and turn on Dark theme", "step": 4, "step_total": 12, "started_at": "2026-10-08T03:15:02Z", "thumbnail_url": "/api/images/20261008_101502_a1b2c3d4_step4.png"},
      "queue": {"total": 4, "items": [{"session_id": "20261008_101502_e5f6a7b8", "goal": "Log in with the demo account", "requested_by": "qa-a@example.com", "position": 1}]},
      "recent": {"items": [{"session_id": "20261008_091200_c0ffee00", "goal": "Check the cart total", "finished_at": "2026-10-08T02:20:41Z", "execution": "completed", "verdict": "fail", "review": "pending"}]}
    },
    {
      "device": {"device_id": "dev_01J9Z3K4M8", "label": "Pixel 3a", "model": "Pixel 3a", "owner_id": "qa-b@example.com", "identity": "confirmed", "active_connection": {"connection_id": "con_01J9Z3Q7", "kind": "wifi", "host_id": "host_01J8X9", "serial": "192.168.1.20:5555"}, "connection_count": 1},
      "state": "busy",
      "last_seen_at": "2026-10-08T03:14:58Z",
      "attention": [],
      "running": {"private": true},
      "queue": {"total": 0, "items": []},
      "recent": {"items": []}
    }
  ]
}
```

- `state`: `running`, `busy`, `idle`, `disconnected` or `unknown`. `busy` means
  a run the caller cannot read; `unknown` means no heartbeat within
  `ARTEMIS_DEVICE_TTL_S` and is never shown as Idle. The UI shows
  "Needs attention" when `attention` is not empty.
- `attention[].reason` and its actions: `review_pending` (`accept`, `reject`),
  `execution_interrupted` (`retry`, `dismiss`), `disconnected_mid_run`
  (`dismiss`), `queue_interrupted_by_restart` (`retry`), `step_stalled`
  (`stop`, `dismiss`; after `ARTEMIS_STEP_STALL_S`).
- `execution` uses the legacy `status` values. `verdict` and `review` use the
  CHE-1331 vocabulary and are `null` until CHE-1331 lands.
- `label` is the device name. The client renders "your PC" or "<Name>'s PC"
  from `owner_id` and `host_id`; the server never sends that text.
- Private run: `running` is `{"private": true}` and the run is left out of
  `queue` and `recent` and their counts
  ([busy lane rule](board-permissions.md#permission-matrix)).
- Lane order: pinned first, then the caller's own devices, then location, then
  `label` alphabetically. New devices append. A `disconnected` lane stays,
  greyed, until dismissed or until `ARTEMIS_LANE_HIDE_AFTER_H` passes.

### Lane preference

```bash
curl -sS -X PUT "$ARTEMIS/api/board/lanes/dev_01J9Z3K4M5/preference" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"pinned": true, "hidden": false}'
```

### Devices

```bash
curl -sS "$ARTEMIS/api/inventory/devices?my_devices=false" -H "$AUTH"
curl -sS "$ARTEMIS/api/inventory/devices/dev_01J9Z3K4M5" -H "$AUTH"
curl -sS "$ARTEMIS/api/inventory/devices/dev_01J9Z3K4M5/timeline?window_h=168" -H "$AUTH"
```

Device detail:

```json
{
  "device_id": "dev_01J9Z3K4M5",
  "canonical_device_id": "dev_01J9Z3K4M5",
  "label": "Pixel 6 Pro",
  "model": "Pixel 6 Pro",
  "owner_id": "qa-a@example.com",
  "identity": "confirmed",
  "state": "running",
  "connections": [
    {"connection_id": "con_01J9Z3P1", "kind": "usb", "source": "host", "host_id": "host_01J8X2", "serial": "1A2B3C4D5E6F", "state": "connected", "last_seen_at": "2026-10-08T03:15:01Z"},
    {"connection_id": "con_01J9Z3P2", "kind": "wifi", "source": "host", "host_id": "host_01J8X2", "serial": "192.168.1.14:5555", "state": "disconnected", "last_seen_at": "2026-10-07T11:02:13Z"}
  ],
  "uncertain_matches": []
}
```

- `kind`: `usb`, `wifi`, `bridge`, `avd`. `source`: `local` (server adb),
  `bridge` (browser WebUSB), `host` (host agent).
- A merged-away id answers with the surviving device and its
  `canonical_device_id` ([aliases](device-identity.md#aliases)).
- Past runs of a device: `GET /api/runs?device_id=dev_01J9Z3K4M5`.
- Timeline: `window_h` is `24` or `168`, default `ARTEMIS_TIMELINE_WINDOW_H`.
  Response `{"intervals": [{"state", "start", "end", "session_id"?}]}` with the
  device states above; server or host downtime is an `unknown` interval.
- Identity actions take `{"other_device_id": "..."}` (merge, keep-separate) or
  `{"connection_id": "..."}` (split). Rules: [device identity](device-identity.md#merge-keep-separate-and-split).

#### `/api/devices` deprecation

`GET /api/devices` keeps its current response shape, pinned by a contract test
because `artemis-client` reads it. The duplicate registrations
(`routers/tasks.py`, `routers/replay.py`) collapse into one handler with the
same shape. Responses add `Deprecation: true` and
`Link: </api/inventory/devices>; rel="successor-version"`. No removal date is
set in CHE-1332.

### Run search

`GET /api/runs` keeps its existing parameters (`q`, `status`, `device`, `host`,
`requester`, `since`, `until`, `cursor`, `limit`) and adds, in Cheese's priority
order:

| Parameter | Filter |
| --- | --- |
| `q` | Goal text (existing FTS5 `runs_fts`). |
| `include_notes` | `true` also matches annotation and comment text. Default `false`. |
| `device_id` | Repeatable; aliases resolve. |
| `verdict`, `status`, `review` | Three separate filters: application verdict, execution status (legacy `status`), QA review. |
| `app_build` | App build snapshotted at execution. |
| `model` | Repeatable; agent model snapshotted at execution. |
| `since`, `until` | The UI defaults `since` to now minus `ARTEMIS_RUN_SEARCH_DEFAULT_DAYS`; the API default stays unbounded for existing clients. |
| `suite` | Test suite (CHE-1339 layer). |
| `requester` | Who ran it. |
| `scope` | `mine`, `available`, `all`; see [scopes](board-permissions.md#scopes). |

Snippets are built after the visibility join. Unknown historical values stay
`null` and match no value filter. When FTS5 is unavailable the response carries
the existing `search_fallback_substring` warning.

```bash
curl -sS -G "$ARTEMIS/api/runs" -H "$AUTH" --data-urlencode 'q=dark theme' \
  --data-urlencode 'scope=available' --data-urlencode 'verdict=fail' \
  --data-urlencode 'device_id=dev_01J9Z3K4M5' --data-urlencode 'include_notes=true'
```

### Notes

Annotations anchor to evidence; comments form one plain-text thread per run.

```bash
# Step anchor
curl -sS -X POST "$ARTEMIS/api/runs/20261008_091200_c0ffee00/annotations" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"anchor": {"kind": "step", "step_id": "stp_01J9Z4A1"}, "body": "Tap lands on the banner, not the button."}'
# Recording anchor: session-relative milliseconds
curl -sS -X POST "$ARTEMIS/api/runs/20261008_091200_c0ffee00/annotations" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"anchor": {"kind": "recording", "recording_id": "rec_01J9Z4B2", "offset_ms": 42000}, "body": "Spinner never stops here."}'
curl -sS -X PATCH "$ARTEMIS/api/runs/20261008_091200_c0ffee00/annotations/ann_01J9Z4C3" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"resolved": true}'
curl -sS "$ARTEMIS/api/runs/20261008_091200_c0ffee00/annotations/ann_01J9Z4C3/evidence" -H "$AUTH"
curl -sS -X POST "$ARTEMIS/api/runs/20261008_091200_c0ffee00/comments" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"body": "Reproduced on Pixel 3a too."}'
```

Annotation:

```json
{
  "annotation_id": "ann_01J9Z4C3",
  "session_id": "20261008_091200_c0ffee00",
  "author": "dev@example.com",
  "anchor": {"kind": "recording", "recording_id": "rec_01J9Z4B2", "offset_ms": 42000, "label": "Recording · 00:42"},
  "body": "Spinner never stops here.",
  "resolved": false,
  "created_at": "2026-10-08T03:20:00Z",
  "edited_at": null,
  "deep_link": "/runs/20261008_091200_c0ffee00?annotation=ann_01J9Z4C3"
}
```

- Step anchors use the stable step id and render "Step 12". Recording anchors
  use the recording id plus session-relative milliseconds and render
  "Recording · 00:42". A recording without a session-relative clock uses the
  legacy fallback and resolves as `legacy_uncertain`.
- The evidence resolver answers
  `{"status": "exact" | "missing" | "expired" | "legacy_uncertain", ...}`. Only
  `exact` returns a media URL. It never snaps to a neighbouring frame.
  `expired` keeps the note text; the UI shows "Recording expired".
- Body: plain text, 1 to 4000 characters, rendered as text (no HTML or
  markdown). Longer bodies answer FastAPI's 422 validation error.
- Rate limit: 30 notes (annotations plus comments) per caller per minute;
  beyond that the existing `rate_limited` 429.
- Deep links open through the run route, so an unreadable run shows
  "Run not found or not shared with you".
- No notifications in v1 (T3 A).
- Who may add, edit, resolve and delete: [permission matrix](board-permissions.md#permission-matrix).

## Live updates (SSE)

Board events ride the existing `GET /api/stream`. The named-session stream
`/api/stream/{session_id}` does not carry them.

```bash
curl -sS -N "$ARTEMIS/api/stream" -H "$AUTH" -H 'Accept: text/event-stream'
```

```text
id: b7f3c2e1:17
event: device.state_changed
data: {"boot_id":"b7f3c2e1","seq":17,"version":1843,"device_id":"dev_01J9Z3K4M8","from":"idle","to":"busy","at":"2026-10-08T03:15:04Z"}

id: b7f3c2e1:18
event: lane.updated
data: {"boot_id":"b7f3c2e1","seq":18,"version":1844,"device_id":"dev_01J9Z3K4M8","lane":{"device":{"device_id":"dev_01J9Z3K4M8"},"state":"busy","running":{"private":true},"queue":{"total":0,"items":[]},"recent":{"items":[]},"attention":[]}}

id: b7f3c2e1:19
event: queue.updated
data: {"boot_id":"b7f3c2e1","seq":19,"version":1845,"device_id":"dev_01J9Z3K4M5","queue":{"total":3,"items":[{"session_id":"20261008_101502_e5f6a7b8","goal":"Log in with the demo account","requested_by":"qa-a@example.com","position":1}]}}

id: b7f3c2e1:20
event: run.annotated
data: {"boot_id":"b7f3c2e1","seq":20,"version":1846,"session_id":"20261008_091200_c0ffee00","annotation_id":"ann_01J9Z4C3","change":"resolved"}

id: b7f3c2e1:21
event: stream.resync
data: {"boot_id":"b7f3c2e1","seq":21,"reason":"buffer_overflow"}
```

| Event | Payload | Visibility check |
| --- | --- | --- |
| `device.state_changed` | `device_id`, `from`, `to`, `at` | Caller may see the device. |
| `lane.updated` | `device_id`, `lane` (snapshot lane shape) | Caller may see the device; the lane is filtered per caller as in the snapshot. |
| `queue.updated` | `device_id`, `queue` | Caller may see the device; items filtered per caller. |
| `run.annotated` | `session_id`, `annotation_id`, `change` (`created`, `updated`, `resolved`, `deleted`); no note text | Caller may read the run. |
| `stream.resync` | `reason` | Always delivered; carries no data. |

Ordering protocol:

- `boot_id` names the server process. `seq` counts the events delivered to
  this subscriber, from 1, without holes: an event filtered out for this
  caller does not use a number. `version` is the server's board event counter.
- The snapshot `watermark` is read under the same lock as the event counter.
  The client applies an event only when its `version` is greater than the
  snapshot's `watermark.version` for the same `boot_id`. An older snapshot
  never overwrites a newer event.
- A new `boot_id` makes the client reload the snapshot exactly once.
- Each subscriber has a bounded queue of 256 events. On overflow the server
  drops the queue and sends `stream.resync`; the client reloads the snapshot.
  A gap is never inferred from filtered events.
- There is no `Last-Event-ID` replay. A reconnect reloads the snapshot.
- Deny by default: a registry maps each event type to its visibility check.
  The server refuses to broadcast a type with no entry and logs it.
- Polling: the board URL parameter `?live=poll` forces polling of
  `GET /api/board` every 5 seconds. The client also falls back to polling when
  the stream drops and shows "Live updates lost".

## Errors

New routes answer errors in the existing `{detail, code, fix}` envelope plus
`docs_url`, which points to the matching entry below:

```json
{
  "detail": "This phone is no longer available to you.",
  "code": "device_claimed",
  "fix": "Pick another device, or ask its owner to share it.",
  "docs_url": "https://github.com/cheese-work/artemis/blob/main/docs/board-api.md#device_claimed"
}
```

The showcase UI has one Angular adapter that maps legacy `{error, detail}`
bodies to `{code: error, detail, fix: null, docs_url: null}`. The UI keeps rows
and drafts after any error.

### Error catalog

The CHE-1332 "Errors" decision defines these eight codes.

#### device_identity_unresolved

`409`. The action needs a confirmed device and this device is uncertain or
provisional, or a Merge spans two different owners. Fix: the device owner
chooses Merge or Keep separate; for a provisional device, authorize USB
debugging or finish Wi-Fi pairing. See [device identity](device-identity.md#troubleshooting).

#### device_claimed

`409`. The single-target form of the fan-out status `claimed`: the device is no
longer available to the caller (another owner holds it, or it was unshared).
Also answered when a Merge or Split touches a device with running or queued
work. Fix: pick another device; retry Merge or Split when the device is idle.

#### device_offline

`409`. Existing code, now also on the new routes. The device has no live
connection. Fix: reconnect the phone, then retry.

#### run_not_visible

`404`. The run does not exist, or the caller may not read it, or a non-owner
used a prefix. The body never says which. Fix: open the full run link, or ask
the person who shared it to share the run or the device.

#### evidence_expired

`410`. The run is kept but its media passed retention. Notes stay readable.
Fix: none for the media; read the note text and step list.

#### annotation_anchor_invalid

`422`. The step id is not a step of this run, the recording id is not this
run's, or `offset_ms` is outside the recording. Fix: pick a step or a moment
from this run's evidence.

#### comment_not_permitted

`403`. The caller may read the run but may not perform this note action, for
example edit or delete another person's note. Fix: ask the note's author or an
administrator.

#### host_unreachable

`503`. The computer that hosts the device does not answer. Fix: check that the
host agent runs on that computer, then "Retry now".

### Codes added or reused by this contract

#### submission_conflict

`409`. Not in the CHE-1332 list: added for the Queue decision "a changed
payload with the same key conflicts". A `POST /api/queue` reused a
`client_submission_id` with a different request. Fix: send a new
`client_submission_id` for a new request.

Reused unchanged: `invalid_scope` 400, `scope_all_requires_admin` 403,
`not_run_owner` 403 (control actions), `device_not_yours` 403 (Merge, Keep
separate or Split by a non-owner), `rate_limited` 429 (notes).
