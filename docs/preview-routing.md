# Native preview routing (L4b1)

CHE-1292 adds code and disposable-fixture evidence only. It does not install a
service, relocate the live listener, change Cloudflare, invoke Docker, admit a PR,
or import an image. `import_preview` always raises `NotImplementedError`. L4b2 owns
the hostile-archive/import boundary; L5 owns admission; L7 owns live cutover.

## Protected configuration

`scripts/preview_routing.py` renders native Traefik configuration. JSON is written
as YAML (JSON is a YAML subset), so no additional serialization dependency is
needed. The pinned Linux/amd64 binary is Traefik **3.6.25**, with SHA-256
`b3fbb7853887c68b23e6c9c418026bed6ed9eab743e5bd3a76c358472bd55b38`.
The upstream release archive was checked against its published checksum before
extracting the binary. Other binaries fail native validation.

- `base_config` contains root UI/API/SSE forwarding, supported-host bounds,
  preview-only ambiguous-path rejection and the reserved preview fallback. A maintainer must
  list every existing supported host explicitly; the default is SmartQA only.
- `preview_config` accepts at most three protected `Preview` registry entries.
  Ports come from the L4a slot registry, not PR arithmetic. Routes require the
  SmartQA Host, exact bare path or slash-delimited prefix, priority 200,
  trailing-slash redirect and StripPrefix. Unknown previews return 404.
- The reserved fallback has priority 100, the real-site fallback 1, and the
  ambiguous-path rejection 1000. Rejection uses native `api@internal` with a
  fixed nonexistent replacement path: this returns 404 without a fallback
  process. No API router or dashboard is exposed. Root `/api/rawdata` and
  `/dashboard/` still belong to Artemis.
- Path sanitization is disabled only with the protected rejection router present.
  Within `/preview` and `/preview/`, dot segments, repeated separators, encoded
  traversal and backslashes cannot select either upstream. Root-site video paths
  with encoded `#`, `/`, `%` or a literal `;` retain their original forwarding.
  Native encoded-character defaults still reject unsafe encodings. Host, Origin
  and the Access assertion are preserved; the application
  still validates the assertion. Native HTTP proxying retains SSE and upgrades.
  Forwarded headers are trusted only from loopback. Bare-path redirects use the
  fixed SmartQA HTTPS origin; native RedirectRegex does not infer its scheme from
  forwarded headers. No arbitrary request host can become a redirect target.

The root-owned provider directory must contain **both** `base.yaml` and
`previews.yaml` directly. Native Traefik watches the top-level directory, not
changes inside subdirectories. `previews.lkg`, `.routes.lock` and temporary
`.candidate` files have no supported provider extension and are ignored.

The deployment owner must provision protected controller code, binary, unit,
static configuration and base file. The dedicated daemon is root-equivalent;
its code writes only preview/backup/lock files, never the base file or unit.
Shared agent identities and previews must have no effective write access.
Use a root-owned `2750` directory with group `artemis-preview-ingress` and
readable `0640` files. Setgid inheritance gives the ingress reader access to new
atomic replacements. Traefik runs as that no-login ingress user, with no Docker
socket. None of these permission or identity prerequisites is claimed installed.

## Publication and recovery

Construct `NativeValidator(binary, base_file, protected_base_sha256)` and
`Reconciler(base_file, provider_directory, protected_base_sha256, validator)`.
The base digest must come from protected reviewed configuration, not be learned
from an untrusted request. Supply the same base pin to both objects.

`reconcile(entries, ready=hook)` checks readiness, serializes the fixed policy,
and runs the checksum-pinned native binary against disposable loopback listeners.
It waits for all expected native routers and rejects disabled/error objects.
The temporary validation API is private to that short-lived fixture process.
The validator carries no ambient credentials or Traefik environment overrides.

Publication uses an exclusive file lock, bounded regular-file/no-link reads,
fsynced staging files and same-directory atomic replacement. The old valid bytes
are saved before the active swap; new valid bytes replace the backup afterward.
Failures before the swap leave the active routes intact. A crash after the swap
leaves either complete valid generation, never a partial active file. Orphaned
`.candidate` files after SIGKILL are ignored; scoped lifecycle cleanup belongs to L6.

Native Traefik retains its running configuration after a bad file reload, but a
fresh start with a malformed file can reject the whole directory. Therefore
`recover` **must finish before every gateway start/restart**. It restores the
validated backup, or an empty preview configuration if both copies are invalid.
It never repairs or replaces a mismatched base. Root routes then load even with
zero usable previews. The checked-in system unit template enforces this order.
Its privileged pre-start recovery is controller work, not ingress authority.

`config/preview/traefik.service` is an **uninstalled template**. Activation needs
the runtime owner's protected Python installation at `/opt/artemis-preview/venv`,
the binary at `/opt/artemis-preview/traefik-3.6.25`, static configuration at
`/etc/artemis-preview/traefik.yaml`, and protected `gateway.conf` containing
`ARTEMIS_PREVIEW_BASE_SHA256`. This is not permission to install or start it.
Real root/tailnet/Access regression and CHE-785 cutover guards remain L7 gates.

## Reproducible checks

```bash
python -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host/test_routing.py -q
ARTEMIS_TEST_TRAEFIK=/path/to/verified/traefik python -m pytest \
  --confcutdir=tests/integration/preview_host tests/integration/preview_host/test_routing_native.py -q
```

Without the pinned binary, native integration tests explicitly skip. Fixtures
allocate their own loopback ports and synthetic HTTP/SSE upstreams; they do not
contact a live service, provider, device or Docker daemon. Coverage includes PR
7/70 boundaries, redirects and headers, root UI/API/SSE, malformed/ambiguous input,
failed swaps, a killed reconciler, corrupted dynamic files and pre-start recovery
followed by a fresh native gateway. This is not live deployment acceptance.
