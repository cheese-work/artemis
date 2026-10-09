# Runner/workspace contract (L5b1)

This layer supplies an **inactive** workflow, allocation/identity checks,
exclusive workspace creation, and transient-unit command plans. It does not
register a runner, install accounts/root filesystems, start systemd services,
build PR code, seal an artifact or publish a preview. The workflow's literal
`if: ${{ false }}` remains until the combined activation gates are satisfied.
No runner environments or agent credentials are inspected.

## Scheduling and admission

`scripts.preview_runner.verify_group()` takes the native runner-group object
and the complete, paginated selected-repository list, fetched by the runtime
owner/control service. It requires:

- Group `artemis-preview-controller`, selected-repository visibility, no
  inheritance, and enforced workflow restrictions.
- Exactly `cheese-work/artemis` at the protected numeric repository ID.
- Exactly `cheese-work/artemis/.github/workflows/preview-controller.yml@<full
  protected controller SHA>`. No mutable branch or other workflow is allowed.
- Public-repository access only if the selected native repository is public.
  The flag never widens the selected repository list. The Artemis fork was
  observed public for this layer; disabling the flag would prevent its jobs.

The workflow selects that group and **all** of `self-hosted`, `cheese-x99` and
`cheese-x99-artemis-preview`. It has no PR trigger or candidate checkout. Only
the installed protected controller executes; `id-token: write` belongs to
that trusted controller, never the PR build sandbox.

`allocate()` requires an unexpired L5a3 dual-anchor admission matching the
current protected policy fingerprint and full candidate tuple. Native job
metadata must match repository, full workflow ref/SHA and group, with bounded
positive run, attempt, job and runner IDs. The `Job` object is a normalized
**trusted-reader contract**, not proof of a GitHub fetch or signed OIDC token.
The daemon must revalidate both approval anchors immediately before dispatch.
Never deserialize caller-supplied metadata as trusted `RunnerPin`, `Job` or
`Allocation` objects. The remaining native job cross-checks, OIDC signature,
audience, expiry, PID/cgroup and transport checks belong to L5c1.

Runner-group creation/administration, selected-workflow SHA restriction
readback and real other-repository/workflow/commit scheduling denial are
**NOT-RUN** and remain runtime-owner duties. The workflow pin must point to
reviewed protected `main`; changing the controller requires independent review
and a protected policy/group-pin update. A fixture PASS is not provisioning.

## Exclusive run tree

The privileged runtime allocates two distinct reserved host UIDs per run:
one for the trusted controller and another for the PR builder. Neither may
be root, the shared agent UID, another active job or a reused account with
unquiesced processes/storage. The numeric range check is not an OS account
reservation mechanism. The runtime owner must establish dedicated accounts,
matching primary GIDs, disjoint subordinate UID/GID ranges and no unintended
supplementary groups. No such host changes occur in this layer.

`prepare_workspace()` walks protected ancestors using held descriptors and
`O_NOFOLLOW`, then locks the root directory. Its fixed production root is
`/var/lib/artemis-preview-runs`, owned by root with mode 0700. Paths derive
only from validated repository/run/attempt IDs and a fresh 128-bit random
nonce. UID, GitHub job, single-use runner and repository/run/attempt claims
are created once and synced before the tree becomes usable. A reused UID,
runner, job or run attempt fails even with a new nonce.

```text
/var/lib/artemis-preview-runs/                     root:root 0700
  uid-<controller UID>/                           protected claim
  uid-<builder UID>/                              protected claim
  job-<repository>-<run>-<attempt>/                protected claim
  github-job-<job ID>/                             protected claim
  runner-<runner ID>/                             protected claim
  <repository>-<run>-<attempt>-<nonce>/            root:root 0700
    controller/                                  root:root 0700
      home/ work/ runtime/                       controller UID:GID 0700
    builder/                                     root:root 0700
      home/ work/ runtime/ state/                 builder UID:GID 0700
    handoff/                                     builder UID:GID 0700
```

Only the privileged service manager bind-mounts each job's leaf directories
into its isolated root filesystem. Root-owned private parents prevent other
host UIDs from traversing the tree, even if a builder changes its leaf modes.
They also prevent leaf renaming/symlink substitution before privileged bind
mounts. No persistent shared `/_work`, DinD tree or writable cross-run cache
is mounted. The handoff leaf is untrusted builder output, **not** a sealed
digest record. L5c2 owns quiescing writers and authenticated sealing/import.

Failures deliberately retain partial trees and claims; retries never silently
reuse them. A separately authorized cleanup path must stop and verify the
exact cgroups, unregister the single-use runner, remove only that allocation's
tree, and establish no remaining UID references before any UID reclamation.
Job/runner identity tombstones must remain protected against replay. L6 owns
lifecycle/TTL cleanup. No cleanup or account-reuse API is exposed here.

Alternate root/anchor/owner arguments exist only for disposable fixtures.
There is no CLI exposing path overrides. UID/GID changes are mocked in unit
tests; actual cross-UID ownership/access denial remains **NOT-RUN**.

## Inert service plans

`controller_command()` and `builder_command()` return argument lists, never
execute them. They supersede the L5a1 runner template's placeholder
`DynamicUser` launch: do not enable that template as this layer's execution
mechanism. The runtime owner must install reviewed, immutable, root-owned
controller and builder root filesystems at
`/usr/local/lib/artemis-preview-controller` and
`/usr/local/lib/artemis-preview-builder`. These roots and trusted executables
are **not** supplied/provisioned by this layer.

The controller gets only its fresh home/work/runtime directories and the
protected `/run/artemis-preview/controller.sock`. It has no builder state,
handoff tree, Docker socket, policy or daemon credential mount. Supplementary
membership in `artemis-preview-runner` grants transport reachability, not
admission. `check_identity()` rejects wrong host UID or any run/attempt/job/
runner/admission/controller-SHA/nonce mismatch. That check is necessary but
**not** sufficient authorization; signed OIDC and native PID/cgroup checks
remain mandatory in L5c1. There is no TCP transport in this layer.

The builder gets only its own work/home/runtime/state and handoff leaves.
Its root filesystem contains no controller token, runner credential, host
daemon socket or agent home. RootlessKit launches BuildKit as the separate
non-root UID, with a run-private Unix socket and native snapshotter. Process
sandboxing remains enabled. `--oci-worker-net=host` refers to the enclosing
RootlessKit namespace, whose network is `none`, **not** the host network.
`IPAddressDeny=any` is an additional systemd constraint. No network-enabled
builder is provided; L5b2/L5b3 own controlled acquisition and offline builds.

Rootless UID/GID mapping needs reviewed `newuidmap`/`newgidmap` helpers and
subordinate ranges inside the trusted builder root. Consequently the builder
plan does not set `NoNewPrivileges=yes`; it bounds helper capabilities to
`CAP_SETUID CAP_SETGID`. The controller uses `NoNewPrivileges=yes` and an
empty capability set. The runtime owner must verify user namespaces, mapping
helpers, immutable root contents, systemd mount/cgroup behavior and socket
readiness before activation. No shared Docker group or sudo authority is
granted to either job. Real RootlessKit/BuildKit/systemd execution is
**NOT-RUN**; a returned command list is not execution evidence.

## Verification

```bash
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host/test_runner.py -q
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host -q
```

Fixtures cover exact pins, public/private repository handling, other
repositories/workflows/SHAs, invalid admission/candidate substitution, shared
or duplicate UIDs, run/runner/job replay, protected-parent/link/traversal
rejection, partial failure retention, identity/peer denial and narrow mount
plans. These are author checks, not an independent verdict or activation
authorization. Live provisioning, scheduling denial, cross-UID checks,
rootless builds, sealing, cutover and preview acceptance remain **NOT-RUN**.
