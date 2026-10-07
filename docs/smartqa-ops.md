# SmartQA Admin and Provider Setup

## Access

`ARTEMIS_AUTH_MODE` defaults to `open`. In this mode, only direct loopback
requests without forwarded-client headers receive admin access; non-loopback
clients do not receive admin access. Route tiers still determine which public
and QA operations they can use. For SmartQA behind Cloudflare Access, set
`ARTEMIS_AUTH_MODE=cloudflare`, `ARTEMIS_CF_ACCESS_TEAM_DOMAIN`,
`ARTEMIS_CF_ACCESS_AUD`, and a comma-separated `ARTEMIS_ADMIN_EMAILS` allowlist.
If unset, the initial allowlist contains `congvc.dev@gmail.com`; setting the
variable to an empty value disables Cloudflare admins.
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
