# SmartQA Admin and Provider Setup

## Deploy version

The QA deployment source and durable admission contract are documented in
[Deploy drain](deploy-drain.md).

Release deployments supply one timestamp through `DEPLOYED_SHA` or
`ARTEMIS_DEPLOYED_AT`. `GET /api/system/version` keeps `status`, `sha`,
`short_sha`, and the UTC `deployed_at`, and adds the display-only `build`
stamp (`YYYYMMDD-hhmm`, ICT / UTC+7). The footer formats the same timestamp
with an explicit `+0700` timezone, independent of the browser timezone.
No timestamp is generated for local or PR builds. Missing deploy metadata
returns an unknown version and `build: null`. A valid SHA without a usable
timestamp keeps the SHA fallback; out-of-range time conversions do not fail
the endpoint.

## Access

`ARTEMIS_AUTH_MODE` defaults to `open`. In this mode, only direct loopback
requests without forwarded-client headers receive admin access; non-loopback
clients do not receive admin access. Route tiers still determine which public
and QA operations they can use. For SmartQA behind Cloudflare Access, set
`ARTEMIS_AUTH_MODE=cloudflare`, `ARTEMIS_CF_ACCESS_TEAM_DOMAIN`,
`ARTEMIS_CF_ACCESS_AUD`, and a comma-separated `ARTEMIS_ADMIN_EMAILS` allowlist.
There is no default admin: with the variable unset or empty, Cloudflare mode has
no admins. Spaces (`ARTEMIS_SPACES_ENABLED=1`, see
[spaces contract](spaces-contract.md)) key admins by Cloudflare subject instead:
read your `subject` from `GET /api/system/whoami` and list it in
`ARTEMIS_ADMIN_SUBJECTS`; the email list is then ignored, and open mode only
admits direct loopback callers.
The service verifies the signed `Cf-Access-Jwt-Assertion` against the team
JWKS, issuer, and audience before using its email claim. Forwarded email headers
are not trusted.

Requests without a valid Cloudflare JWT, including tailnet requests that bypass
Cloudflare Access, do not receive an authenticated identity. The `/api/run`,
`/api/stop`, and `/api/resume` task controls and the ADB WebSocket bridge remain
public-tier. Provider and device mutations, including replay, require admin
identity; the approved ADB key-healing and emulator-dismissal recovery actions
require a signed QA identity. Granting admin access to a remote tailnet client
requires an explicitly approved authenticated route; do not treat network
reachability as identity. Server shutdown remains a separate loopback-only
lifecycle-token operation.
Optional `/api/v1/*` cloud routes retain their tenant bearer-token auth and are
classified separately from Cloudflare admin routes.

Every registered HTTP and WebSocket route has an explicit tier. Read routes,
task controls, and the ADB WebSocket bridge are public-tier. Configuration,
provider/device mutations, replay, and shared ADB restart require admin identity;
ADB key healing and emulator dismissal require QA identity. Keep route-tier
tests current when adding routes.

## Provider Configuration

The Provider Setup page reads the active model defaults and reports credential
presence with a four-character suffix only. It never returns a stored key.
Writes require an admin identity and an unchanged configuration version.
`ARTEMIS_ARTEMIS_JSONC`, when set, selects the active `artemis.jsonc` file;
otherwise the normal Artemis path resolution applies. The selected config and
the writable `.env` must be outside the source checkout and writable by the
service account.

Credential and base-URL values supplied by the service environment are
read-only. Config responses omit secret-like model fields and strip userinfo,
query strings, and fragments from existing base URLs. Other changes are
validated with Artemis's runtime LLM parser, then
written under a process lock to `.env` followed by `artemis.jsonc`. Prior files
are backed up, failures trigger rollback, and a secret-free audit record is
appended to `artemis-admin-audit.jsonl` beside `.env`.

Each newly spawned task receives a private snapshot of the active config and
dotenv values. A successful save applies to future runs without restarting the
service; already-running workers keep their original snapshot. Readiness checks
only the providers referenced by the active model configuration. Vision OCR is
optional and is reported separately.

## Computers (release B, off by default)

Computers share their phones with SmartQA. Set `ARTEMIS_HOST_AGENT=enabled` on the server to turn the feature on (`1`, `true`, `yes` and `on` also work, case-insensitive; anything else, or unset, is off); with the flag off, Setup → Computers says "Computers are turned off on this server" and every `/api/agent/*` route answers 404.

- **Who can do what.** Anyone signed in reads the list. Admins create enrollment codes, rename and revoke. These are `/api/hosts*` routes, behind Cloudflare Access like every other human route.
- **Cloudflare path policy.** The computer software calls `/api/agent/*` without a person signing in, so Access must let that prefix through (bypass, or an Access service token). Each route proves itself in the application: an enrollment code, a key signature, or a session token. Add a WAF rate limit for the prefix. `ARTEMIS_ALLOWED_HOSTS` needs no new entry; the software uses the existing public hostname.
- **Enrollment codes.** 128-bit, single use, 15 minutes, stored hashed, rate limited per IP and per code. A code binds to the first key that signs it; the same key may retry within the 15 minutes, any other key gets "used".
- **Sessions.** A signed challenge (single-use nonce, 60 s) opens a 24-hour token bound to the computer and its connection. Reconnecting replaces the earlier connection. Revoking closes the live connection at once and refuses renewal and uploads.
- **Rollback.** Turn the flag off and revoke computers. The registry tables (`hosts`, `host_devices`, `host_tokens`, `host_enrollment_codes`) are additive and ignored when the flag is off.

## Failures (admins)

Setup → Failures lists failed steps and failed or interrupted runs of the last 14 days, grouped by cause: `smartqa_infra` and `smartqa_agent` (SmartQA's to fix), `provider` (model gateway), `user_prompt` (shown, "No action"), `qa_environment` (QA setup), `app_defect` (app under test) and `unknown`. QA setup and app defects are not counted as SmartQA defects. Classification is rule-based (`services/failure_ledger.py`); the ledger is in the run catalog database, collected hourly and on demand (`POST /api/system/failures/collect`, which also backfills existing runs).

Each chat turn has its own session/trace ID. Classification reads the full unwrapped terminal error saved by `task.finalize(error=...)` through the lifecycle authority, or that turn's `status.json`. A nonempty structured error takes precedence even when it matches no rule. The stdout tail is a legacy fallback for that exact turn only; a multi-turn chat's shared worker log is not safe fallback evidence. Never substitute another turn's error.

Interrupts persist `{reason, client_close.reason, visibility_state}` in the lifecycle transaction and project the same fields into `status.json`. Rules inspect fields, not concatenated diagnostic text: keyguard locked errors produce `qa_environment/device_locked`; `bridge_closed` with client reason `usb_disconnected` and visibility `hidden` produces `qa_environment/usb_disconnected_hidden` before the generic interruption rule. A visible tab, missing visibility, or a deploy close 1012 remains `smartqa_infra/run_interrupted`; `auth_expired` is `smartqa_infra/auth_expired`, not QA setup.

`collect(session_id=None)` scans history by default; passing the exact turn ID limits collection to that turn and remains idempotent. Classifier-version and latest-classification precedence belongs to the A2b consumer/migration layer, not this evidence layer.

- **Daily digest.** Once a day, one message lists `smartqa_*` causes not reported before, sent through the notifier channels (`ARTEMIS_WEBHOOK_URL` and the others in `mcp_server/notifiers`). A cause counts as reported only after a channel accepted the message; with no channel configured, the digest repeats daily and `POST /api/system/failures/digest` returns `"sent": false`.
- **Evidence** is redacted and cut to 300 characters. Run causes use structured per-turn terminal errors and interrupt evidence; the `stdout.log` tail is a legacy fallback only when structured evidence is absent.

## Agent entry point: `smartqa check` (CHE-2101)

`smartqa check --apk app.apk --manifest smartqa.yml --themes light,dark [--pool x99-avd | --device <id>] [--route route.json] [--server <url>]`
ships with `packages/artemis-client`. Per theme it leases a device, submits one run and
releases the lease. Exit 0 = every theme completed, 1 = a run failed or the check was refused,
2 = usage error or missing credentials.

- **Auth.** Non-loopback servers need `CF_ACCESS_CLIENT_ID` + `CF_ACCESS_CLIENT_SECRET` (or
  `ARTEMIS_TOKEN`); the CLI refuses to start without them. The client id must be mapped in
  `ARTEMIS_SERVICE_PRINCIPALS`; routes may use `{name}` for one path segment, for example
  `"GET /api/sessions/{session_id}"`. Map `POST /api/smartqa/apks`, `POST /api/leases`,
  `POST /api/leases/{lease_id}/release`, `POST /api/run` and `GET /api/sessions/{session_id}`.
- **APK upload.** `POST /api/smartqa/apks` (raw body, optional `X-Content-SHA256`) stores the
  file as `<sha256>.apk` under `ARTEMIS_SMARTQA_APK_DIR` (default `<test data>/smartqa_apks`).
  Cap: `ARTEMIS_SMARTQA_APK_MAX_BYTES` (default 200 MiB). The endpoint is 404 unless
  `ARTEMIS_SMARTQA_CHECK=1`.
- **Manifest.** `smartqa.yml`: `package`, `screens` (`id`, `purpose`), `test_accounts`, optional
  `backend`. `backend: production` is refused (`prod_backend_refused`). The parser reads plain
  and simply quoted scalars only; tags, anchors, block scalars, escapes and quoted keys are
  refused (`manifest_invalid`), never guessed.
- **Spend and fence.** One check spends at most 80 model calls in total (an equal share per
  theme; a route spends none). The lease is released in two cases only: (a) the task ended
  by itself with a status the worker writes (`completed` or `failed`), or (b) the host
  provably never admitted it (a rejection, a local `ValueError`, or an HTTP 4xx other than
  408). On every other path (poll error, timeout, cancellation, an unclear submit, a
  `cancelled` status) the client sends `stop` best-effort and keeps the lease until its TTL.
  It never polls for a terminal status to justify a release: the server writes `cancelled`
  before the worker exits, and the API has no later exit signal. A cancelled check
  therefore holds the device until the lease TTL.
- **Route.** `{"steps": [{"action": "tap|swipe|back|type", ...}]}`. A `type` step with
  `"secret": true` must carry a `${secret:NAME}` placeholder (`route_secret_literal` otherwise).
- **Server contract still to land.** The run receives `options.smartqa` (`theme`,
  `fresh_install`, `clear_data`, `mode` persona|route, `route`, `max_model_calls`). Applying it
  (`cmd uimode`, `pm clear`, route replay) belongs to the worker and the restricted command
  gate (CHE-2129); the lease endpoints belong to CHE-2099.
