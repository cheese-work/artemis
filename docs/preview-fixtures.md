# Synthetic preview data and Access validation

CHE-1290 adds the L3 layer on PR 92. The preview still uses the real FastAPI
read and ownership routes. No JWT fake or identity switcher is installed.
Normal-mode authentication and storage are unchanged.

## Required configuration

Select `ARTEMIS_PREVIEW_PROFILE=1` before importing the server. Set:

- `ARTEMIS_APP_DIR`: absolute fixture parent on the preview's private writable
  mount. Each process creates a new mode-0700 child. Existing databases, traces
  and dotenv files in the parent are not adopted.
- `ARTEMIS_AUTH_MODE=cloudflare`, `ARTEMIS_CF_ACCESS_TEAM_DOMAIN` and
  `ARTEMIS_CF_ACCESS_AUD`: the existing Access application and issuer.
- `ARTEMIS_PREVIEW_QA_EMAILS`: exactly two distinct, comma-separated admitted
  QA email identities. Whitespace and case are normalized.
- `ARTEMIS_ADMIN_EMAILS`: exactly one explicit admin email, distinct from both
  QAs. The normal-mode default admin is not a preview configuration.
- `ARTEMIS_PREVIEW_JWKS_BUNDLE`: absolute path to a host-refreshed, read-only
  **public** signing-key bundle. Never mount a private signing key or credential.

The fixture parent and `TMPDIR` must be bounded private tmpfs mounts when the
runtime sandbox is delivered in L4. This layer does not install mounts, network
rules, a host refresher or a container. It runs only against disposable data.

## Public-key bundle contract

The trusted host publishes this JSON envelope atomically:

```json
{
  "issuer": "https://example.cloudflareaccess.com",
  "fetched_at": 1791360000,
  "keys": [
    {"kty": "RSA", "kid": "issuer-key-id", "alg": "RS256", "use": "sig",
     "n": "base64url-public-modulus", "e": "AQAB"}
  ]
}
```

`fetched_at` is the host's successful key-fetch time, in Unix seconds. The
example is a schema illustration, not a usable key bundle. The configured
issuer must match exactly. A future timestamp or a bundle older than 3600
seconds is refused. The file must be regular, not a symlink or FIFO, and at
most 65536 bytes. There must be 1–16 public RSA signing keys with unique key
IDs; private key fields and unsupported algorithms are refused.

`CloudflareAccessVerifier` performs the existing RS256 signature, issuer,
audience and required-claim validation. Its injected fetcher reads only this
bundle and never performs HTTP. Preview JWKS caching is disabled so a removed,
rotated or expired bundle fails closed on the next request, including after a
successful request from the same identity. A missing or invalid initial
bundle prevents boot. Every allowed preview request requires a signed QA or
admin identity. Unsigned email headers cannot establish identity.

## Fixture and control behavior

Startup seeds nine runs: queued, running and completed for each QA and admin.
IDs, timestamps and synthetic goals are deterministic. No real device IDs,
worker PIDs, host paths, prompts, screenshots or provider results are copied.
Storage APIs create the private SQLite schema and record real run ownership.
An existing database is refused rather than reset or overwritten. A new
process creates a fresh dataset, so redeploy resets the preview.

Stop and queued cancellation update only the synthetic queue and database.
If outcome persistence fails, the control returns 503 without changing the
in-memory item or reporting success.
Resume changes only the preview pause flag and preserves the existing
all-affected-runs ownership check. The owner or admin can delete a finished
run through the legacy session route. The run-library delete route still
requires admin. A live or pinned fixture cannot be deleted. Deletion uses
the storage API without filesystem deletion, process probes or retention work.

Pin, unpin, clear and retention writes remain `preview_disabled`. Device,
provider, host, lifecycle and real task-execution routes remain disabled.
The existing browser caches stay scoped to preview prefix and verified
identity; identity changes clear displayed sessions and the previous cache.

## Local verification and external limits

Backend checks live in `tests/unit/admin_console/test_preview_fixtures.py`.
They generate ephemeral RSA keys in tests, sign tokens and exercise the real
verifier and ownership routes. The fresh-process boot probe traps network and
process calls and preserves a foreign database sentinel. Existing profile and
route-enumeration checks remain required. Browser cache regressions live in
`agent.service.spec.ts` and `browser-storage.service.spec.ts`.

No deploy, Cloudflare policy change, device access or provider call is part of
this layer. Live Access acceptance still requires the real host key-refresh
path, runtime isolation, admitted QA accounts and independent review. Local
synthetic signed-token checks are not evidence of live Cloudflare acceptance.
