# Sealed preview image import (CHE-1293, L4b2)

This layer is inert. It installs nothing and admits no live preview. The only
CLI operation checks an archive on standard input. There is no CLI import or
route publication operation. The module uses only the Python standard library.

## Manifest schema v1

`scripts/preview_import.py:parse_manifest` accepts at most 16 KiB of UTF-8 JSON.
The following keys are mandatory; unknown or duplicate keys fail closed:

| Keys | Constraint |
| --- | --- |
| `schema_version`, `repository` | Integer `1`, literal `cheese-work/artemis` |
| `repository_id`, `run_id`, `run_attempt`, `job_id`, `runner_id` | Positive integers below `2**63`; booleans and floats are not integers |
| `pr` | Positive 31-bit integer |
| `head_sha`, `base_sha`, `controller_sha` | Full lowercase 40-hex commit SHAs |
| `nonce`, `archive_sha256` | Lowercase 64-hex values |
| `admission_id` | Canonical UUID |
| `archive_size` | Positive integer, at most 4 GiB |
| `image_id` | `sha256:<64 lowercase hex>` config digest, never a tag |

Canonical bytes are sorted JSON with compact separators and no final newline.
The protected seal contains the entire validated manifest plus SHA-256 of these
canonical bytes. Formatting changes do not alter the seal. Every identity,
candidate, archive and image value must match the seal. A builder-supplied hash
is only a comparison value, not authority to create a seal.

## Unprivileged pre-check

The default checker launches `/usr/bin/bwrap`, with private user, PID, network,
IPC, UTS and mount namespaces; UID/GID 65534; no capabilities; no ambient
environment; read-only system libraries and checker code; and disposable
`/tmp`, `/proc` and `/dev`. The archive arrives only through an inherited
read-only standard-input descriptor, never a caller-supplied path. Bubblewrap
applies its no-new-privileges behavior; no host socket or writable host mount
is provided. Timeout or namespace failure stops import; there is no unsandboxed
fallback. The checker runs with a 30-second wall timeout, 15-second CPU budget,
256 MiB address-space limit, 32-descriptor limit, zero file-write limit and no
core dumps. Production checker code and interpreter must be protected installs,
not code obtained from the candidate PR.

The supported export is **one tagless, uncompressed legacy Docker-save image**
for Linux/amd64. Export by image ID, not a mutable name. Required outer members
are `manifest.json`, `<config-sha256>.json` and `<layer-id>/layer.tar`. Declared
layer directories and bounded legacy `json`/`VERSION` metadata are accepted.
OCI-layout, compressed exports, multiple images, tags, image-declared volumes, undeclared files,
duplicate names, outer links/special files, sparse entries and nonzero trailing
data are rejected. The controller/export layer must target this format; an OCI
archive requires a separately reviewed format extension, not a fallback.

The checker never extracts files. It validates the config digest/platform and
ordered uncompressed layer DiffIDs. Layer headers reject traversal, absolute
member names, escaping relative links, sparse entries and special files.
Container-root absolute symlinks are permitted. Metadata is at most 1 MiB per
file, with at most 128 layers and 65,536 members per archive/layer. Both the
archive and the sum of layer file sizes are capped at 4 GiB. These checks do not
claim semantic equivalence to Docker's parser or parser isolation after import.

## Protected import seam

`import_sealed` is disabled by default. Its trusted caller supplies an already
held, read-only descriptor for a daemon-owned, singly linked regular file with
mode `0400`, a protected `Seal`, an admission revalidation hook, and a runtime
owner's version/artifact receipt. It accepts no archive filename, socket path,
runtime option, image tag or caller-defined route. The protected parent directory
and writer quiescence are prerequisites, not properties mode bits alone prove.
L5c2 owns quiescence, held-descriptor copying and create-once sealing. Do not call
this seam on the builder's output file or construct its seal from job metadata.

Import verifies the seal, engine pin, ownership/type/size, archive SHA-256 and
the sandbox result. It rechecks metadata and SHA-256 after the pre-check and
revalidates admission immediately before load. The same descriptor is rewound
and passed to the loader. `DockerLoad` uses a fixed host Docker socket, empty
client configuration, no ambient Docker environment and a fresh server-version
check. Load has a 120-second client timeout; inspect requires the exact sealed
config/image ID. Production must use the default sandbox checker; injectable
checker/loader hooks exist for disposable unit fixtures and trusted adapters.

No import operation changes active, last-known-good or protected base routes.
A load failure, stale admission or wrong image ID cannot publish a route.
Successful import returns only an immutable image ID; the controller must still
apply L4a's fixed runtime policy and L4b1's readiness/atomic routing checks.
Partial Docker objects after a failure belong to L6's exact-ID cleanup. Client
timeout does not prove cancellation of daemon-side work; the future controller
must reconcile its result and must never publish a timed-out candidate.

## Docker pin and activation gate

Pinned Docker Engine: **29.8.2**, published September 30, 2026. This release
includes security fixes, including crafted OCI descriptor graph resource
exhaustion (CVE-2026-53493) and registry TLS/DNS downgrade (CVE-2026-92543).
The official release was read during this task; this is a dated pin, not a claim
that future releases stay supported automatically.

Official Docker image `docker:29.8.2-dind`:

```text
OCI index: sha256:7dcdfc4a20246236f558175182ccace1eb15a41bd3eb119dd2284f393498b7c1
Linux/amd64 manifest: sha256:dcac6f16dc25ddec91e2d467605775b95a035ab884b94cb4c2cc7cbef6fd726d
```

Source: `https://github.com/moby/moby/releases/tag/docker-v29.8.2` and the
official `registry-1.docker.io/v2/library/docker/manifests/29.8.2-dind` response.
The index response's SHA-256 matched its registry content-digest header. The
amd64 manifest was selected by its explicit platform, not by tag alone.

Before enabling import, the X99 runtime owner must install/verify the pinned
artifact, attest the actual host daemon's source and effective server version,
and record that receipt in protected state. The digest is a provisioning anchor,
not permission to run privileged DinD here, use the shared CI DinD engine, or
replace L4a's host bridge/firewall model. If provisioning uses image contents as
the host installation source, record extracted binary hashes and service command
as well. No engine installation, upgrade or privileged host change occurs here.
Versions other than the exact pin and mismatched artifacts are blocked. Review
security support and patch advisories again before activation; changing the pin
requires review and fresh checks.

`docker load` still parses hostile data in rootful `dockerd`. The dedicated daemon
is root-equivalent, and the pre-check does not remove that residual risk. All
admission, human approval, runner isolation, seal, resource, host-readiness and
CHE-785 cutover gates remain mandatory. Neither an injected fixture PASS nor
the pinned image's existence proves those gates have passed.

## Disposable verification

```bash
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host/test_import.py -q
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host -q
```

Unit fixtures cover hostile manifests/tars/layers, substituted identities,
digest mismatch, unsafe sealed files, stale admission, sandbox failure/timeouts,
fixed Docker commands, and failed-import route preservation. The checker process
test uses a synthetic archive and the current unprivileged UID. Mocked namespace
and Docker command tests do not establish native namespace or rootful-import
acceptance. The native namespace test is opt-in and uses no Docker daemon:

```bash
ARTEMIS_TEST_ARCHIVE_SANDBOX=1 python3 -m pytest -o addopts='' \
  --confcutdir=tests/integration/preview_host \
  tests/integration/preview_host/test_import_sandbox.py -q
```
