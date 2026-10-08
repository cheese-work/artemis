# Synthetic preview data and Access validation

CHE-1290 adds the L3 layer on PR 92. The preview still uses the real FastAPI
read and ownership routes. CHE-1336 adds an opt-in synthetic identity source
for agent-run acceptance. Normal-mode authentication and storage are unchanged.

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
  **public** signing-key bundle for signed-Access mode. The identity-switch mode
  below does not read or require this bundle. Never mount a private signing key
  or credential.

The fixture parent and `TMPDIR` must be bounded private tmpfs mounts when the
runtime sandbox is delivered in L4. This layer does not install mounts, network
rules, a host refresher or a container. It runs only against disposable data.

## Agent-run identity switch (CHE-1336)

Set `ARTEMIS_PREVIEW_IDENTITY_SWITCH=1` (or `true`) before starting an isolated
preview. The switch defaults off; `0` and `false` leave signed-Access mode in
place. Invalid values fail boot. Enabling it without `ARTEMIS_PREVIEW_PROFILE=1`
fails before lifecycle imports, so the production profile cannot use it.
Request headers, cookies and later environment edits cannot select a profile.
The existing private fixture directory, disabled device/process/provider routes,
and per-preview sandbox remain required.

Keep the cloudflare ownership configuration and explicit three fixture emails
above. Use synthetic addresses such as `qa-a@example.test`, `qa-b@example.test`
and `admin@example.test`; no admitted mailbox or signing credential is needed.
The aliases `qa-a`, `qa-b` and `admin` map to those fixed fixture owners, never
to a caller-supplied email. `whoami` reports `auth_mode: "preview"` so synthetic
identity cannot be mistaken for a verified Access assertion.

For API acceptance, send `X-Artemis-Preview-Identity: qa-a` (or `qa-b`/`admin`).
For browser acceptance, seed `artemis_preview_identity` in each separate browser
context before navigation. Set the cookie path to the preview's base path
(`/preview/pr/<n>/` through ingress, `/` with a root-base local UI build).
Reload after changing a context's identity; browser caches already scope data
by preview prefix and identity. For example, with an existing Playwright runner:

```javascript
const origin = new URL(previewUrl);
for (const alias of ['qa-a', 'qa-b', 'admin']) {
  const context = await browser.newContext();
  await context.addCookies([{
    name: 'artemis_preview_identity', value: alias,
    domain: origin.hostname, path: origin.pathname,
    httpOnly: true, sameSite: 'Strict', secure: origin.protocol === 'https:'
  }]);
  const page = await context.newPage();
  await page.goto(previewUrl);
    await context.close();
}
```

No selector gives 401 on protected preview routes. Unknown aliases, duplicate
identity headers or conflicting header/cookie selectors give 400. A selector
on any non-opted-in app gives 403 `preview_identity_disabled`, even with a valid
Access assertion. In identity-switch mode JWT assertions are not a fallback;
requests must select a fixture identity. QA ownership and admin restrictions
still apply through the existing route dependencies.

**Cloudflare changes: none for X99-local acceptance.** The sandbox already
publishes each preview upstream only on `127.0.0.1:18100+slot`. Use that origin
for direct API checks. For a prefixed browser build, use the existing private
Traefik listener with `/preview/pr/<n>/` and the SmartQA Host preserved, not the
unprefixed container port. The preview owner supplies the recorded listener,
slot and URL; Chromium's per-run host resolver can map `smart-qa.tevo.vn` to
`127.0.0.1` for the private listener without changing system DNS or Access.
Alternatively use a root-base local UI build with the isolated backend. Run
from X99 or through an already-authorized private tunnel; do not expose the
port or change the firewall, ingress or Access policy. The public SmartQA origin still needs
Access sign-in; this switch does not bypass the Cloudflare edge. Public-edge
acceptance without sign-in is not delivered by this mode.

The trusted preview owner passes the opt-in environment value through
`scripts/preview_sandbox.py` when creating its preview. This PR changes only the
builder's allowlist, not deployed configuration. The builder still requires its
read-only public-key mount argument; fixture mode does not consume that mount.

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
bundle prevents boot in signed-Access mode. Every allowed request in that mode
requires a signed QA or admin identity. Unsigned email headers cannot establish
identity. Opted-in identity-switch mode instead uses only the fixed aliases.

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
this layer. Identity-switch tests also boot the real preview without a key
bundle, trap network/process calls and exercise three identities and ownership
denials. Production-profile and missing-opt-in selectors are refused. Live
Access acceptance still requires the real host key-refresh
path, runtime isolation, admitted QA accounts and independent review. Local
synthetic signed-token checks are not evidence of live Cloudflare acceptance.
