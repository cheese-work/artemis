# SmartQA Admin and Provider Setup

## Access

`ARTEMIS_AUTH_MODE` defaults to `open`. In this mode, only direct loopback
requests without forwarded-client headers receive admin access; non-loopback
clients are read-only. For SmartQA behind Cloudflare Access, set
`ARTEMIS_AUTH_MODE=cloudflare`, `ARTEMIS_CF_ACCESS_TEAM_DOMAIN`,
`ARTEMIS_CF_ACCESS_AUD`, and a comma-separated `ARTEMIS_ADMIN_EMAILS` allowlist.
If unset, the initial allowlist contains `congvc.dev@gmail.com`; setting the
variable to an empty value disables Cloudflare admins.
The service verifies the signed `Cf-Access-Jwt-Assertion` against the team
JWKS, issuer, and audience before using its email claim. Forwarded email headers
are not trusted.

Requests without a valid Cloudflare JWT, including tailnet requests that bypass
Cloudflare Access, remain read-only. This includes task submission, stop/resume,
provider/device mutations, replay, and the ADB WebSocket bridge. Granting admin
access to a remote tailnet client requires an explicitly approved authenticated
route; do not treat network reachability as identity. Server shutdown remains
a separate loopback-only lifecycle-token operation.
Optional `/api/v1/*` cloud routes retain their tenant bearer-token auth and are
classified separately from Cloudflare admin routes.

Every registered HTTP and WebSocket route has an explicit tier. Read routes are
public-tier; task controls, configuration, device, cleanup, restart, delete,
replay, and the ADB WebSocket bridge require an admin identity. Keep route-tier
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
