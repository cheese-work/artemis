# Board permissions and run visibility

Contract for CHE-1332 (device board, run search, notes). Gate decision T4 A:
runs stay **unlisted**, not secret. This file is the single definition of the
run scopes, the caller classes and the permission matrix. The other board docs
link here and do not restate them.

Related: [board API](board-api.md) · [device identity](device-identity.md) ·
[migrations, config and contributor map](board-operations.md)

## Caller classes

| Class | Who |
| --- | --- |
| Run owner | Signed-in caller whose email is the run's `requested_by`. |
| Link holder | Signed-in caller who is not the owner and opens the run by its **full** `session_id`. |
| Other signed-in caller | Signed-in, not the owner, has no full id for the run. |
| Device owner | Owner of the device the run used. Owning the device gives no extra run access; this row is the same as "Other signed-in caller" unless the caller is also the run owner. |
| Admin | Signed-in caller listed in `ARTEMIS_ADMIN_EMAILS`. |
| Unauthenticated | No verified identity. In `ARTEMIS_AUTH_MODE=cloudflare` every board route refuses this caller through the existing access tier. |

In `ARTEMIS_AUTH_MODE=open` the scope is not enforced (existing
`scope_or_open` behaviour): every caller acts as the run owner. The rules below
apply to `cloudflare` mode and to the opted-in preview identities.

Identity headers (`Cf-Access-Jwt-Assertion`, and `X-Artemis-Preview-Identity`
in an opted-in preview) are trusted only behind the authenticating proxy. Do
not expose the console port directly.

## Permission matrix

| Capability | Run owner | Link holder | Other signed-in caller | Admin |
| --- | --- | --- | --- | --- |
| **Discover runs**: run list, search, snippets, counts, board lanes, live updates | Yes | Yes, after the first full-id open (see `available`) | No | Yes, with `scope=all` |
| **Read evidence and notes**: run detail, recordings, images, manifests, bundles, named-session streams, annotations, comments | Yes | Yes | No: `run_not_visible` 404 | Yes |
| **Add notes**: annotations and comments | Yes | Yes | No | Yes |
| **Edit own notes**: edit text, delete, toggle Resolved on an annotation you wrote | Yes | Yes, while the run stays readable | No | Yes, and may delete any note |
| **Control execution**: stop, cancel a queued item, retry, resume, QA review Accept/Reject | Yes | No: `not_run_owner` 403 | No | Yes |

Rules that apply to every row:

- A capability never implies another one. Comment permission never implies
  control.
- **Resolved toggle:** any caller who may add notes may toggle Resolved on any
  annotation of that run (T3 A: a simple shared toggle, no notifications).
- **Lost visibility:** a note stays on the run after its author loses read
  access. The author can no longer read, edit or delete it; the run owner and
  admins still see it.
- **Busy lane, private run:** a caller who sees a device but cannot read the
  run on it sees `Busy · private run` only. The payload carries no goal, owner,
  thumbnail, step or queue detail for that run. This applies to the board
  snapshot and to every SSE event.
- **No existence leak:** an unreadable run answers `run_not_visible` 404, the
  same as an unknown id. Never 403, never 410, never a 409 candidate list.
  Search never reports hidden-result counts.

## Scopes

`scope` is a query parameter on run lists, search, the board and the stream.

| Scope | Returns | Who may use it |
| --- | --- | --- |
| `mine` | Runs whose owner is the caller. Default. | Every signed-in caller |
| `available` | `mine` plus every run the caller has opened by its full `session_id` (link-shared runs). | Every signed-in caller |
| `all` | Every run, including unowned runs. | Admins only; others get the existing `scope_all_requires_admin` 403 |

An unknown value gets the existing `invalid_scope` 400.

**Link-shared, v1 definition.** A run enters the caller's `available` set when
the caller reads it successfully by its full `session_id` (run detail or a
direct evidence URL). The server records the pair (caller, `session_id`) at that
moment. A prefix lookup never records anything. CHE-1332 states "own runs plus
runs shared by link" but does not fix how the server learns about a shared
link; this record is the mechanism the permission layer implements. Teams
(CHE-1333) add further members to `available` later, additively.

**Existing `scope=everyone`.** `GET /api/runs?scope=everyone` (CHE-1276) stays
as shipped: a redacted, read-only catalog. Board, search filters, notes and
SSE do not accept `everyone`. Whether `everyone` is later folded into
`available` is a separate decision; this contract does not change it.

### Board toggles

The board and the run list map the two UI toggles onto these scopes.

| Parameter | `true` (default) | `false` |
| --- | --- | --- |
| `my_runs` | `scope=mine` | `scope=available` |
| `my_devices` | Lanes for devices the caller owns | Lanes for every device available to the caller: owned, shared, and unowned shared devices |

- Values are `true` or `false`. Omitted or blank means `true`. Any other value
  answers [`request_invalid`](board-api.md#request_invalid).
- The two toggles are independent. `my_devices` alone decides which lanes
  appear. `my_runs` decides only which runs fill those lanes (`running`,
  `queue`, `recent`) and the counts. `my_devices=false` with `my_runs=true`
  therefore shows every device available to the caller, including a shared
  idle phone the caller never used; its lane shows only the caller's own runs.
- Lane `state` always describes the device, never the filter. A device running
  a run that `my_runs=true` hides is `busy`, never `idle`. A run the caller
  may not read keeps the `Busy · private run` marker and its redaction under
  either toggle value.
- An explicit `scope` wins over `my_runs`. `scope=all` therefore needs admin.
- Repeated `device_id` parameters filter to those devices (OR). A merged-away id
  resolves through its alias (see [device identity](device-identity.md#aliases)).
- Counts (`lane_count`, `run_count`, `queue.total`) count only what the caller
  may discover under the active scope.
- In `open` mode the toggles still filter by the (absent) owner, so
  `my_devices=true` shows every device and `my_runs=true` shows every run.

## Prefix rule

The existing 8-character prefix lookup on `GET /api/runs/{session_id}`:

1. Visibility is applied **before** candidate selection: the candidate set is
   only the caller's own runs (admins: every run).
2. Visibility is applied **before** the 404 vs 410 decision: a removed run the
   caller could not read answers `run_not_visible` 404, not 410.
3. A non-owner always needs the full `session_id`. A full id resolves under the
   matrix above.

## Authorization closure

Every route that returns run data applies the matrix, not only the run detail.
Today these routes are open to any signed-in caller by link
(`core/access_control.py` `_PUBLIC_GET_PATHS`):

- run detail and prefix resolution: `/api/runs/{session_id}`,
  `/api/sessions/{session_id}` and its sub-resources (`steps`, `events`,
  `tree`, `plan`, `notes`, `checks`, `usage`, `startup_progress`)
- recordings: `/api/sessions/{session_id}/video`, `/videos/{video_path:path}`
- images: `/api/sessions/{session_id}/goal-images/{index}`,
  `/images/{image_name}`, `/api/images/{image_name}`
- manifests, traces and bundles: `/api/steps/{step_id}/traces`,
  `/api/traces/{trace_id}`, `/api/traces/{trace_id}/download`,
  `/api/runs/{session_id}/bundle.zip`
- named-session streams: `/api/stream/{session_id}`

Evidence named only by an image name, a file path, a trace id or a step id
(`/images/*`, `/api/images/*`, `/videos/*`, `/local_file`, `/api/steps/{id}/traces`,
`/api/traces/{id}` and its `/download`) carries no run id in the URL. The caller must own a
live run that owns the file, or have opened such a run by its full `session_id`
first. Guessing a path proves nothing. Every other miss (removed, ownerless,
unknown) is the same `run_not_visible` 404 for a non-admin.

The existing `/api/sessions/{session_id}/notes` route is the agent's own notes,
not the annotations and comments defined in [board API](board-api.md#notes).

Each one answers `run_not_visible` 404 to a caller without read access. Every
new route is listed in both the access-control registry and the preview route
registry (`core/preview_routes.py`), and a test fails when a route is missing
from either.
