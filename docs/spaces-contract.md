# Spaces contract

Contract for CHE-1333 (personal and shared spaces, roles, device ownership).
Cheese admitted CHE-1333 on 2026-10-08 22:22 ICT. This file is the single
definition of principals, delegation, enqueue defaults, verbs, error codes,
scopes, the revocation bound, the privacy rules and the migration plan. Later
stages implement it; they change this file first.

**Revision 3.** Revision 2 resolved the five P1 design findings of CHE-1385 comment
`01a11c49-de30-7b7c-9013-1f7ced2dbb64`. Revision 3 corrects P1-1 (email-change history) and P1-5 (grant-derived listing) after the re-check in comment `01a11c76-cbd5-74f5-966d-f7f3743c2f82`. See [Review disposition](#review-disposition).

Related: [board permissions](board-permissions.md) · [device identity](device-identity.md) ·
[board operations](board-operations.md)

## Status per stage

| Stage | Slice | State |
| --- | --- | --- |
| 1 | Principals, delegations table, `SystemPrincipal`, fail-closed actor, open-mode refusal, retryable 503, admin by subject | Partly implemented in CHE-1385 (PR 128). Code gaps: [Stage 1 conformance](#stage-1-conformance) |
| 2 | Spaces, members, built-in roles, `can()`, SQL scope, admin override, revocation epochs, enqueue `space_id`, credential binding | Specified here |
| 3 | Device lend/return/reclaim, admission fencing, system space; migration | Specified here |
| 4 | UI, docs, demo | Specified here |

### Enablement gate

`ARTEMIS_SPACES_ENABLED=1` is a pre-release switch for the identity layer
(stage 1). **Do not set it in a deployment that serves users** until stages 2
and 3 ship and every gate in [Release gates](#release-gates) passes. An
identity-only layer gives no privacy: it must not be offered as spaces.
Without the flag the console behaves as before, except that the hardcoded
default admin email is gone.

## Principals

A principal is one verified identity, keyed by `(issuer, sub)`. Email is
contact data and never a key.

| Field | Meaning |
| --- | --- |
| `id` | Opaque id used by every other table, including run ownership. |
| `issuer`, `sub` | The key. `UNIQUE (issuer, sub)`. The same `sub` under another issuer is another principal. |
| `kind` | `user`, `agent` or `service`. |
| `email` | Latest verified email, lower-cased. Not unique. Contact data only. |

Email history lives in its own table, `principal_emails`:

| Field | Meaning |
| --- | --- |
| `email` | Primary key. Lower-cased. One row per address, across all issuers. |
| `principal_id` | The first principal that presented this address in a verified login. |
| `contested` | `1` once a different principal has presented the same address. Never reset by code. |

#### Email history policy

- Every verified email a principal presents is recorded in the **same `BEGIN IMMEDIATE` transaction** as the login or the email change. The first presenter reserves the address.
- Reservations stay with their principal. An email change adds a reservation for the new address and keeps the old one: principal A who moves from `old@` to `new@` still holds `old@`.
- If a different principal presents an address that another principal reserved, in the same transaction the address becomes `contested` and the presenter reserves nothing. The reserved row keeps its `principal_id`.
- Because a principal that is the **current** holder of an address always reserves it at presentation, no address can be both current for one principal and free for another.
- A legacy run row (principal id NULL) whose email is `contested` is owned by **no one**. Stage 3 puts it in the quarantine list. Only a global-admin override reads it. Ambiguity is quarantined, never guessed.
- Required case (principal A changes `old@` to `new@`, then principal B first signs in as `new@`): A holds `old@` and `new@`. B reserves nothing and `new@` becomes `contested`. B never owns A's runs filed under `new@`, and A does not own B's.

### Namespaces

| Namespace | Issuer | Sub | May reserve an email, bind a pending membership or be global admin |
| --- | --- | --- | --- |
| Real | The Cloudflare Access team issuer | The JWT `sub` | Yes |
| Preview fixture | `urn:artemis:preview` | `preview:<alias>` | No. A fixture identity never reserves an email, never binds a pending membership and never matches `ARTEMIS_ADMIN_SUBJECTS`. |
| Open mode, anonymous | none | none | No principal exists. |

### Ownership follows the principal

- A run records `requested_by_principal` (principal id) beside the legacy `requested_by` email. Enqueue writes both. From the stage 2 reader release, ownership is decided by principal id.
- A legacy row (principal id NULL) is owned only by the principal that reserved the row's email in `principal_emails`, and only while that email is not `contested`. Same email with another `sub`, or the same `sub` under another issuer, owns nothing.
- `OwnerScope` carries the principal id, issuer and subject. An email match alone never proves ownership while spaces are enabled.
- Binding the legacy email is atomic: it happens in the first-insert transaction under `BEGIN IMMEDIATE`, so concurrent first logins yield exactly one holder.

### Pending membership

A member added by email is a `pending` row with `pending_email`, `expires_at` and no principal.

- Expiry is **30 days** from creation. The Admin can edit, remove or re-add it. The UI shows "Pending N days".
- Binding happens once, in one transaction on a verified login: `UPDATE … SET principal_id = ?, status = 'active' WHERE lower(pending_email) = ? AND status = 'pending' AND principal_id IS NULL AND expires_at > now`. One affected row means bound. An expired, already-bound or removed row never binds.
- The first verified `(issuer, sub)` wins. A later subject with the same email, in any issuer, gets nothing and sees `membership_pending` 403 if it asks for that space.
- The email must come from the verified token. Preview fixture identities never bind.

### Global admins

Global admins are keyed by subject: `ARTEMIS_ADMIN_SUBJECTS` (comma-separated `sub` values of the configured Access team issuer). With spaces enabled it is the only source. `ARTEMIS_ADMIN_EMAILS` stays a legacy source while spaces are off. No default admin exists. An install with neither variable has no admin.

**Global-admin designation is not data access.** It grants exactly two things: platform routes behind the `admin` tier (configuration, ADB, restart, retention) and the right to invoke an override. See [Global admin override](#global-admin-override).

## Authorization model

`can(principal, space, ability)` is default-deny. Abilities are a closed code enum served by `GET /api/abilities`; unknown keys are rejected. Three sources can allow an action, and none implies another:

1. **Space membership** with a built-in role (Admin, QA, Dev, Runner). A space Admin manages one space only.
2. **Resource grant**: the per-run link grant (CHE-1332). It allows reading that run's evidence and nothing else.
3. **Override**: a global admin's audited, reasoned, time-boxed action.

Space kinds: `personal` (one member, cannot be shared), `shared` (creator becomes Admin), `system` (custodians are global admins, every verified user is a Runner).

### Global admin override

- A global admin with no membership and no grant cannot read or change an unrelated run, device, stream or file. The answer is the concealed 404 `access_denied`, the same as for anyone else.
- An override is a request that names the resource, the action and a `reason` (1 to 500 characters). Without a reason: `admin_override_reason_required` 422.
- The server writes the `audit_events` row **before** the action takes effect, in its own committed transaction. If the write fails, the action is refused with a retryable 503. An override covers one action on one resource; it is not a session mode.
- The UI shows the "Admin override" banner for the action. Space-Admin actions (members, lend, return) are ordinary role abilities and are never recorded as overrides.
- The old blanket rule (`OwnerScope.may_act_on` true for any admin, browser phones visible to any admin) is removed when spaces are enabled.

### SystemPrincipal

`SystemPrincipal` is the server acting for itself: queue workers, retention, startup recovery, migrations and tests. It is an internal authority, not a fallback.

- Only trusted in-process entry points hold it. It is never built from a request field, a header, the peer address, loopback status or a failed dependency injection.
- A missing or wrong-type actor is denied (`actor_required` 401). There is no unscoped default.
- Router modules must not reference `SYSTEM_PRINCIPAL`. A test fails if any module under `apps/admin_console/routers` does.
- `SystemPrincipal` still honours the retryable 503: it never returns raw data when the catalog is not ready.

### Open mode

Open mode with spaces enabled is allowed only when **both** hold:

1. **Bind.** The server is bound only to a loopback address (`127.0.0.1` or `::1`). Any other bind (including `0.0.0.0`) with spaces enabled refuses to start.
2. **Request.** The direct peer is loopback **and** the request carries none of `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `CF-Connecting-IP` (the set in `core/access_control.py`, `_FORWARDING_HEADERS`). Otherwise `open_mode_loopback_only` 403 (WebSocket: close 1008).

A loopback bind behind a forwarding proxy therefore fails the request check and never becomes anonymous resource access.

### Retryable 503

`CatalogNotReady` and a missing principal store answer 503 with `Retry-After` and `retryable: true` on **every** authorization path: HTTP, WebSocket, SSE, bundle, media and the system principal. No path returns raw data, a partial list or an unscoped result when readiness is unknown.

## Delegation

A human authorizes an agent or service to act in listed spaces until a time.

| Table | Columns |
| --- | --- |
| `delegations` | `id`, `agent_principal_id`, `human_principal_id`, `mode` (`read` or `run`; `run` includes `read`), `expires_at`, `revoked_at`, `created_at` |
| `delegation_spaces` | `delegation_id`, `space_id` |
| `agent_credentials` (stage 2) | `id`, `principal_id`, `kind` (`cf_service_token` or `host_token`), `credential_ref`, `revoked_at` |

### Credential binding

- An agent or service authenticates with its own credential. The server resolves the credential to **one** `agent` or `service` principal through `agent_credentials`. A credential that resolves to no principal, or to a revoked row, is `not_signed_in` 401.
- A request field or header such as `on_behalf_of` is a **claim to check, never authority**. The claim is honoured only when a live `delegations` row binds the authenticated agent principal, that human principal, the target space, a non-expired `expires_at` and no `revoked_at`. A forged, expired, revoked or wrong-agent claim is refused with `access_denied` 404.
- A host-agent token (`core/agent_auth.py`) authenticates a computer, not a human. It never carries a delegation by itself.

### Effective permission

Effective abilities = agent's abilities ∩ the human's abilities ∩ the delegation's spaces, evaluated **now**, not when the grant was made. A demotion or removal of either principal narrows or ends the delegation at once.

- **Execution** (start, resume, stop, cancel) needs `mode = run`.
- **Read-back** of a run needs `mode = read` or `run` and is limited to runs the agent submitted in a delegated space. Read-back never grants execution. The enqueue response always returns `space_id`, `audience`, `space_defaulted` and the run id, so the agent can confirm placement without read-back.
- Runs record both principals ("Submitted by agent · on behalf of person").
- Dispatch, start and resume recheck current membership of the human, current abilities of the agent and the live delegation, immediately before acting. A change between enqueue and worker start ends the run as `access_revoked` without starting it.

### Enqueue default order

`POST /api/run` and `ArtemisClient.submit()` resolve the space in this order, and **stop at the first source that names a space**:

1. Explicit `space_id`.
2. The principal's default space.
3. The principal's personal space.
4. Otherwise `space_required` 422.

An explicit space the caller may not run in: `access_denied` 404. A default space the caller may no longer run in: `space_required` 422 with a "default space unavailable" detail. Neither falls through to another space. UI state is never inherited. A run's space is frozen at enqueue.

## Privacy rules

These are contract conditions for stages 2 to 4. The stage 1 flag must stay off for users until they hold ([Enablement gate](#enablement-gate)).

### Revocation bound

`membership_epoch` and `delegation_epoch` are kept per principal and are bumped in the same transaction as the change.

| Item | Value |
| --- | --- |
| Origin of the bound | Commit time of the membership, role or delegation change. |
| Bound | **10 seconds** to the last byte sent on an affected stream or download. |
| Checks | Before **every send**, and on a timer every **5 seconds** on an idle stream. |
| Decision cache | None across checks: the epoch is read from the database at each check. A cached visible-space set is valid only for the epoch values it was built from. |
| Delegation revocation | Bumps `delegation_epoch` of the agent. Any decision cached under the old value is invalid on the next check. |

On a failed check the server (1) purges the connection's queued events, (2) sends one `access_lost` frame (`{type, resource, reason}`), (3) closes: SSE ends the stream, WebSocket closes with 1008. A client must not auto-reconnect after `access_lost`. Active downloads are checked per chunk (at most 1 MiB) and cancelled. Copies already downloaded are out of reach (accepted risk).

### Live view and device use

Device use (`run on this phone`) is **not** permission to watch the screen. Live view is its own ability. A frame is delivered only if the viewer can read the active run **and** the frame's device-assignment generation equals the current one. A mismatch drops the frame and closes the view.

### Media

- A media path with an **empty owner set** is denied (`access_denied` 404), for every caller including a global admin without an override. Today's unleased file for an empty owner set (`routers/media.py`) is a defect that stage 2 removes.
- A media path is allowed only through **any owning run the caller can read**, and it takes the run's lease.
- Clients move to run-scoped media URLs. The path routes remain only as a denied-by-default compatibility layer until removed.

### Link grants (CHE-1332)

The admitted rule stays: anyone signed in with the full run link can open the run, and only the run owner revokes the grant.

- Removing a member does **not** revoke a link grant that member already holds.
- A grant is a **direct resource authorization**, not a listing or search inclusion. It authorizes reading that one run's evidence (detail, media, bundle, annotations) by its full id. It never authorizes execution, never appears in listings, search results, counts or board lanes, and never makes the run `available` by scope.
- The grant branch is evaluated only by the single-run read check (`can_read_run`: owner, or member with the ability, or grant). No list, search, count, queue, stream-scope or stats query reads grants.
- This supersedes the CHE-1332 wording in `board-permissions.md` that adds link-opened runs to `available`. Stage 2 updates that file with the scope change.
- A stream or download opened through space membership closes on removal. The same person can reopen the run through the grant.

### Concealment

Hidden and unknown spaces, runs and devices answer the same concealed 404 `access_denied`. A prefix lookup counts only authorized rows, so ambiguity never reveals a hidden run. Search never reports hidden-result counts.

## Verbs

| Verb | Actor | Effect | Endpoint | Audit event |
| --- | --- | --- | --- | --- |
| `lend` | Device owner | Owner's personal space → shared space | `POST /api/devices/{id}/lend` | `device.lent` |
| `return` | Space Admin | Shared space → owner's personal space | `POST /api/devices/{id}/return` | `device.returned` |
| `reclaim` | Device owner | Same result as `return`; durable state `requested → draining → returned \| failed` | `POST /api/devices/{id}/reclaim` | `device.reclaimed` |

A move is blocked while queued, running or resumable work holds the phone (`device_locked_active_work` 409 with a count). Reclaim offers drain or cancel. A drain timeout escalates to an audited Force release. Moves use compare-and-set on `space_epoch` inside `BEGIN IMMEDIATE`, keyed on the stable `device_id`.

## Error codes

Envelope: `{detail, code, fix}` plus `docs_url`.

| Status | Code | When |
| --- | --- | --- |
| 401 | `not_signed_in` | No verified identity, or a credential that resolves to no live principal. |
| 401 | `actor_required` | A caller supplied no actor. |
| 403 | `open_mode_loopback_only` | Open mode with spaces enabled, request not direct loopback. |
| 403 | `membership_pending` | Member added by email, not yet bound. |
| 404 | `access_denied` | Hidden or unknown space, run, device, media, or a refused delegation claim. Never 403, so existence is not revealed. |
| 409 | `device_not_in_space` | Run asks for a phone outside its space. |
| 409 | `device_locked_active_work` | Move blocked by active work. |
| 409 | `last_admin_required` | Removing or demoting the last space Admin. |
| 422 | `space_required` | No space resolves at enqueue, or the default space is unavailable. |
| 422 | `admin_override_reason_required` | Global admin override without a reason. |
| 503 | `catalog_not_ready` | Run catalog tables missing. Retryable. |
| 503 | `principal_store_not_ready` | Principal tables missing. Retryable. |
| 503 | `audit_unavailable` | Override audit row could not be written. Retryable. |

Every 503 carries `Retry-After` and `retryable: true`. `run_not_visible` 404 (CHE-1332) stays on run routes until stage 2 replaces it with `access_denied` in the same response shape.

## Scopes

`mine`, `everyone` and `all` are replaced by `space`, `mine` and `available` across library, search, queue, stream and stats. The visible-scope predicate runs in SQL: `space_id IN (visible spaces) OR requested_by_principal = me`, with an index on `run_meta(space_id, created_at)`. There is **no grant term** in any listing, search, count, queue, stream or stats predicate.

| Scope | Returns |
| --- | --- |
| `space` | Runs in the selected space, if the caller may see runs there. |
| `mine` | Runs the caller owns, in any space. |
| `available` | Runs in every space where the caller may see runs, plus the caller's own runs. Link grants are not included. |

## Migration plan

1. **Stage 1 (additive).** `principals`, `delegations` and `delegation_spaces` are created with `CREATE TABLE IF NOT EXISTS`. Nothing reads them while spaces are off. Rollback: ignore the tables.
2. **Reader release first.** It enforces `space_id` when present, **denies every non-owner when `space_id` is NULL**, and reads `requested_by_principal` before the email. No writer sets `space_id` yet.
3. **Writer release second.** It writes `space_id` and `requested_by_principal` on every insert and every worker upsert. The old writer cannot insert a row the reader would show to the wrong person.
4. **Backfill.** Runs map from the recorded submitter, devices from established ownership, never from the migration caller. A record without a clear owner, or whose email is `contested`, goes to the **quarantine** list, visible to global admins only through an override. A preview bound to a data version lists access gained and lost before the admin confirms.
5. **Rollback** never returns to `scope=everyone` or to email-only ownership. Otherwise use maintenance mode until forward repair.

## Release gates

Spaces are not offered to users until all of these pass.

| Gate | Owner stage | Proof |
| --- | --- | --- |
| Route coverage | 2 | A test fails when any HTTP route, WebSocket route, SSE route, artifact route or mount lacks the resource authorization dependency. |
| Concealed 404 | 2 | Hidden run, device, space and media answer the same body as unknown ones. |
| Prefix filtering | 2 | An ambiguous prefix counts only authorized rows. |
| Retryable 503 | 1 | A catalog or principal-store failure returns 503 with `Retry-After` on the run list, run detail, task-plan, notes, media and bundle routes. The task-plan route does not turn it into a text body. |
| Revocation | 2 | Removing a member closes an open SSE, bridge WebSocket and agent WebSocket, and cancels a download, within 10 seconds. |
| Delegation | 2 | Forged, expired, wrong-agent and revoked claims fail. A demotion or revocation between enqueue and worker start ends the run unstarted. |
| Admin override | 2 | A global admin without grant or override cannot read an unrelated resource. A missing reason returns 422 and writes no action. |
| Media | 2 | An empty owner set is denied. Run-scoped URLs carry the lease. |
| Link grant scope | 2 | A run opened only through a grant is absent from list, search, counts, board lanes and `available`, and cannot be stopped or resumed. |
| Reader-before-writer | 3 | An old-writer insert and a worker upsert after cutover stay confined. A NULL-space row denies non-owners. |
| Rollback | 3 | After space-scoped writes, rollback keeps confidentiality and never restores `everyone`. |
| Interrupted migration | 3 | An interrupted migration reruns to the same result. |

## Stage 1 conformance

PR 128 at `c5b5cb58eb9b9a6aa75a64a091ad7c873f97750e` implements the principal tables, the delegations tables (without `mode`), `AccessIdentity.issuer/subject`, `SYSTEM_PRINCIPAL`, `require_actor`, the open-mode request check, retryable 503 and admin by subject. This revision adds code work for stage 1. It starts only after the independent design re-check.

1. `OwnerScope` carries the principal id; with spaces enabled a legacy email owns a row only through an uncontested `principal_emails` reservation. `history_email` is replaced by `principal_emails` and the email-change policy.
2. With spaces enabled, `admin` no longer widens `sees` or `may_act_on`, and `scope=all` is refused. Platform `admin` routes are unchanged.
3. Preview fixtures use the `urn:artemis:preview` namespace.
4. The open-mode request check uses the full forwarding-header set; startup refuses a non-loopback bind with open mode and spaces.
5. A test fails when a router module references `SYSTEM_PRINCIPAL`.
6. `delegations` gains `mode`.
7. API-seam tests for the five findings. Email-only fixtures do not stand in for subject-binding tests. Required cases: same email with another subject; same subject under another issuer; concurrent first logins; the A `old@` to `new@`, B `new@` sequence; the task-plan route returning 503 when the catalog is not ready.
8. The task-plan route (`services/media_service.py`) lets a catalog authorization failure reach the API as a retryable 503. Today a catch-all turns it into an error string.

Not in stage 1: spaces, members, `can()`, override action, credential binding, epochs, media rewrite, migration.

## Review disposition

| Finding (comment `01a11c49-…`) | Resolved in |
| --- | --- |
| P1-1 Subject re-key must reach ownership | [Principals](#principals): namespaces, [email history policy](#email-history-policy), ownership follows the principal, pending membership |
| P1-2 No blanket global-admin bypass | [Global admins](#global-admins), [Global admin override](#global-admin-override) |
| P1-3 SystemPrincipal is internal authority | [SystemPrincipal](#systemprincipal), [Open mode](#open-mode), [Retryable 503](#retryable-503) |
| P1-4 Delegation credential binding and live checks | [Delegation](#delegation) |
| P1-5 Privacy contract now | [Enablement gate](#enablement-gate), [Privacy rules](#privacy-rules), [Link grants](#link-grants-che-1332), [Scopes](#scopes), [Migration plan](#migration-plan), [Release gates](#release-gates) |

## Accepted risks

- Saved run links survive member removal until the owner revokes them.
- Copies already downloaded cannot be recalled.
- The audit table shares the SQLite database.
- A reassigned email address that two people have presented is `contested`: its legacy rows are quarantined for both until a global admin resolves them. This can hide a legitimate owner's old runs.
