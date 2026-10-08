# Spaces contract

Contract for CHE-1333 (personal and shared spaces, roles, device ownership).
Cheese admitted CHE-1333 on 2026-10-08 22:22 ICT. This file is the single
definition of principals, delegation, enqueue defaults, verbs, error codes,
scopes, the revocation bound and the migration plan. Later stages implement
it; they change this file first.

Related: [board permissions](board-permissions.md) · [device identity](device-identity.md) ·
[board operations](board-operations.md)

## Status per stage

| Stage | Slice | State |
| --- | --- | --- |
| 1 | Principals, delegations table, `SystemPrincipal`, fail-closed actor, open-mode refusal, retryable 503, admin by subject | Implemented in CHE-1385 |
| 2 | Spaces, members, built-in roles, `can()`, SQL scope, revocation epochs, enqueue `space_id` | Specified here |
| 3 | Device lend/return/reclaim, admission fencing, system space; migration | Specified here |
| 4 | UI, docs, demo | Specified here |

`ARTEMIS_SPACES_ENABLED=1` turns the stage 1 rules on. Without it the console
behaves as before, except that the hardcoded default admin email is gone.

## Principals

A principal is one verified identity, keyed by `(issuer, sub)` from the
Cloudflare Access JWT. Email is contact data and never a key.

| Field | Meaning |
| --- | --- |
| `id` | Opaque id used by every other table. |
| `issuer`, `sub` | The key. `UNIQUE (issuer, sub)`. |
| `kind` | `user`, `agent` or `service`. |
| `email` | Latest verified email, lower-cased. Not unique: a person can change email and two subjects can show the same email. |
| `history_email` | Set once, at first insert, to the email only if no other principal already holds it. `UNIQUE`. It decides which principal may claim email-keyed history (runs with `requested_by = email`). |

Rules:

- `AccessIdentity` carries `issuer` and `subject` beside `email`. `GET /api/system/whoami` returns `subject` so an operator can read the value to configure.
- A principal row is created or refreshed on a verified login while spaces are enabled. A read finds the row; a write happens only when the row is missing or the email changed.
- Historical email-keyed runs link to a principal only through `history_email`: the first verified login of that email, and never when another `sub` already holds it. Stage 3 performs the link; stage 1 only records the holder.
- A missing `sub` is an invalid token (`jwt_invalid`). Open mode, preview fixtures and anonymous callers have no subject and no principal.

### Global admins

Global admins are keyed by subject: `ARTEMIS_ADMIN_SUBJECTS` (comma separated `sub` values, issuer is the configured Access team). With spaces enabled this is the only source. `ARTEMIS_ADMIN_EMAILS` stays a legacy source while spaces are off. No default admin exists: the hardcoded email is removed, and an install with neither variable has no admin.

Global admins have no implicit bypass in space routes (stage 2). An override is an audited action with a reason (`admin_override_reason_required`).

## Delegation

A human authorizes an agent to run in listed spaces until a time.

| Table | Columns |
| --- | --- |
| `delegations` | `id`, `agent_principal_id`, `human_principal_id`, `expires_at`, `revoked_at`, `created_at` |
| `delegation_spaces` | `delegation_id`, `space_id` |

- Effective permission is the **intersection** of the agent's and the human's. A delegation never adds an ability the human lacks.
- A delegation covers a space when it is not revoked, `expires_at` is in the future and the space is listed.
- Runs record both principals ("Submitted by agent · on behalf of person"). A forged, expired or revoked `on_behalf_of` is rejected, and dispatch, start and resume re-check it.
- `delegation_spaces.space_id` has no foreign key until the stage 2 `spaces` table exists.

## Fail-closed actor

- `scope_or_open()` is gone. In-process callers (services and tests with no request) pass `SYSTEM_PRINCIPAL`, an explicit, unenforced `SystemPrincipal`. Any other non-actor value is refused with `actor_required` 401.
- Open mode (`ARTEMIS_AUTH_MODE=open`) with spaces enabled admits only a direct loopback caller with no proxy forwarding header. Anything else gets `open_mode_loopback_only` 403 (WebSocket: close 1008).
- `CatalogNotReady` and a missing principal store answer retryable 503 (`Retry-After`), never an unscoped result.
- Stage 2 adds one resource authorization dependency over every HTTP, WebSocket, artifact, bundle and SSE route, with a route-coverage test.

## Enqueue default order

`POST /api/run` and `ArtemisClient.submit()` resolve the space in this order:

1. Explicit `space_id`.
2. The principal's default space.
3. The principal's personal space.
4. Otherwise `space_required` 422.

UI state is never inherited. The response carries the persisted `space_id`, `audience` and `space_defaulted`. A run's space is frozen at enqueue.

## Spaces, roles, abilities

Kinds: `personal` (one member, cannot be shared), `shared` (creator becomes Admin), `system` (custodians are global admins, every verified user is a Runner).

Built-in roles are fixed in v1: Admin, QA, Dev, and Runner (system space only). `can(principal, space, ability)` is default-deny. Abilities are a closed code enum served by `GET /api/abilities`; unknown keys are rejected. Role table: see CHE-1333, "Built-in role abilities".

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
| 401 | `not_signed_in` | No verified identity. |
| 401 | `actor_required` | A caller supplied no actor. Stage 1. |
| 403 | `open_mode_loopback_only` | Open mode with spaces enabled, caller not direct loopback. Stage 1. |
| 403 | `membership_pending` | Member added by email, not yet signed in. |
| 404 | `access_denied` | Hidden or unknown space, run, device. Never 403, so existence is not revealed. |
| 409 | `device_not_in_space` | Run asks for a phone outside its space. |
| 409 | `device_locked_active_work` | Move blocked by active work. |
| 409 | `last_admin_required` | Removing or demoting the last Admin. |
| 422 | `space_required` | No space resolves at enqueue. |
| 422 | `admin_override_reason_required` | Global admin override without a reason. |
| 503 | `catalog_not_ready` | Run catalog tables missing. Retryable. |
| 503 | `principal_store_not_ready` | Principal tables missing. Retryable. Stage 1. |

Every 503 carries `Retry-After` and `retryable: true`.

`run_not_visible` 404 (CHE-1332) stays on run routes until stage 2 replaces it with `access_denied` in the same response shape.

## Scopes

`mine`, `everyone` and `all` are replaced by `space`, `mine` and `available` across library, search, queue, stream and stats. The visible-scope predicate runs in SQL: `space_id IN (…) OR requested_by = me OR id IN grants`, with an index on `run_meta(space_id, created_at)`. Per-run link grants (CHE-1332) are independent of spaces and are never listed.

## Revocation bound

`membership_epoch` is kept per principal and space. SSE, device-bridge WebSocket and agent WebSocket compare the epoch before each send and at least every **5 seconds**. On a change they purge queued events and close with an `access_lost` frame. Active downloads are cancelled. The bound from removal to a closed stream is **10 seconds**. Copies already downloaded are out of reach (accepted risk).

## Migration plan

1. **Stage 1 (additive).** `principals`, `delegations` and `delegation_spaces` are created with `CREATE TABLE IF NOT EXISTS`. Nothing reads them while spaces are off. Rollback: ignore the tables.
2. **Reader release.** Enforces `space_id` when present and denies non-owners when NULL.
3. **Writer release.** Starts writing `space_id`.
4. **Backfill.** Runs map from the recorded submitter, devices from established ownership, never from the migration caller. Records without a clear owner go to a quarantine list visible to global admins only. A preview bound to a data version lists access gained and lost before the admin confirms.
5. **Rollback** never returns to `scope=everyone`. Otherwise maintenance mode until forward repair.

## Accepted risks

- Saved run links survive member removal until the owner revokes them.
- Copies already downloaded cannot be recalled.
- The audit table shares the SQLite database.
- Email-keyed history can be claimed only by the principal that holds `history_email`; a person who changes email keeps their `sub` but not the old email's history.
