# Preview image and runtime sandbox

CHE-1291 adds the L4a layer on the L3 preview stack (PR 93). Nothing here deploys,
installs a rule or starts a service; the trusted host daemon of a later layer runs
the output of `scripts/preview_sandbox.py`.

## Image

`docker build --target preview -t <name> .` builds the per-PR image. It has no ADB,
git or curl, runs as UID 10001 and sets `ARTEMIS_PREVIEW_PROFILE=1`. The only extra
packages are the shared libraries the OpenCV wheel needs at import time. The default
target (last stage) is still the live console with ADB.

## Run-time options

`container_create_argv` and `network_create_argv` build the `docker create` and
`docker network create` commands. The daemon never takes options from the image:

| Area | Setting |
| --- | --- |
| Identity | `--user 10001:10001`, `--cap-drop ALL`, `no-new-privileges`, default seccomp, read-only rootfs, private IPC and PID |
| Limits | 1 CPU, 1 GiB memory and swap, 256 PIDs, 256 MiB writable tmpfs in total (64 `/tmp`, 160 state, 32 `/dev/shm`), 20 MiB logs |
| Mounts | One read-only bind: the host's public JWKS bundle. No socket, device or host path |
| Ingress | Published to `127.0.0.1:18100+slot` only; Traefik is the sole client |
| Network | Per-slot bridge `artemis-pv<slot>` in `172.31.240.0/24` (one /28 each), no NAT, no ICC, IPv6 disabled, DNS pointed at a blackhole |
| Environment | Five allowlisted inputs plus fixed values the caller cannot override |

The PID limit is 256, not the planned 128 (amended on CHE-1291). The app idles at 106
tasks and peaks at 111 after boot. 93 of them are one `DataEngineHandler._drain_queue`
thread per logger name, created at `import artemis`; reducing them means changing app
code. 128 left 17 PIDs for the request thread pool. Native thread pools (OpenBLAS, OMP,
MKL, NumExpr) are pinned to one thread: without that, `import numpy` alone spawned 55
threads on the 56-core host.

## Checks the daemon must run

- `verify_image(docker image inspect)` before `docker create`. Docker mounts an
  anonymous volume for every image-declared `VOLUME`, even with `--read-only`. That is
  writable host-backed storage outside the tmpfs budget, and no run flag disables it, so
  the image is refused.
- `verify_container(docker inspect)` after create and before `docker start`. The mounts
  must be exactly the read-only JWKS bind plus the two tmpfs.

## Host firewall

`FIREWALL_RULESET` is one nftables table (`inet`, so IPv4 and IPv6). New connections
from any `artemis-pv*` interface are dropped in `input` and `forward`; replies to
host-initiated connections pass. This denies the host gateway and its services,
private, tailnet and metadata ranges, the internet, DNS and other previews. The
daemon must load the table before it starts a preview and must not admit a preview
until `scripts/preview_isolation_probe.py` run inside the container reports every
target blocked. Use `dns-udp://` targets (a valid DNS query) for resolvers: a service can
ignore the generic `udp://` payload and look blocked. A negative DNS answer such as
NXDOMAIN is an answer and fails the probe. Without the table a container can reach the host: Docker alone
does not isolate it.

## Tests

- `tests/unit/preview_host`: hermetic checks of the builders, the image contract and
  the probe.
- `tests/integration/preview_host/test_firewall.py`: loads the ruleset into a real
  kernel inside a disposable container (the container is the host; preview and
  internet namespaces hang off it). A control run with no rules proves each target
  is reachable; with the rules every one is blocked and ingress still works.
- `tests/integration/preview_host/test_container.py`: starts the real image from the
  sandbox argv and checks the engine applied the options, the app serves and rejects
  unsigned requests, and the container has no ADB, a read-only rootfs and no IPv6. A
  hostile image that declares a `VOLUME` is refused. Both modules remove only the Docker
  objects they created, by ID (`owned.py`).

Run the integration tests with `pytest -m integration tests/integration/preview_host
-o addopts=""` after `docker build --target preview -t artemis-preview:l4a .`.
