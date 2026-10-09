# Protected preview control foundation (L5a1)

This layer supplies code, uninstalled systemd templates and disposable-fixture
tests. It does **not** install accounts, start a daemon, register a runner,
inspect credentials, read runner environments or admit a preview. The
[L5b1 runner/workspace contract](preview-runner.md) supplies isolated allocation
and inactive service plans; it supersedes this foundation's runner placeholder.
Receipt
verification and admission belong to [L5a2](preview-receipts.md)/L5a3. The runtime owner provisions
reviewed files only after the remaining implementation and activation gates.

Cheese's Plan A decision on CHE-1283 (2026-10-09) permits X99-hosted previews
without spend and accepts X99 agents as trusted root-origin insiders. It does
not turn a path preview into an untrusted-code sandbox. The daemon remains
root-equivalent through rootful Docker. No claim of isolation from an agent
with effective host-root access is made. Removing that authority, or separately
accepting its risk, is the runtime owner's responsibility, not this layer's work.
CHE-785 cutover acceptance, runner-group enforcement, human-account custody and
live QA acceptance remain separate gates.

## Accounts and units

`config/preview/sysusers.conf` declares no-login `artemis-previewd` and
`artemis-preview-ingress` accounts and the `artemis-preview-runner` access group.
The runner template allocates a dynamic `artemis-prjob-<instance>` identity per
controller run; there is no shared long-lived runner UID. L5b1 must allocate a
bounded numeric instance, record the actual UID/PID/cgroup/run/attempt/nonce and
exclude concurrent reuse. A dynamic UID alone does not authenticate a controller.
L5c1 must verify the Unix peer and OIDC against that same registry.

- `artemis-previewd.service` has Docker authority and private state. Its
  privileged pre-start checks root-owned policy, registry and scope assessment.
  `LoadCredential` delivers an owner-provisioned daemon credential from a
  root-only directory; it never appears in an environment value or a PR image.
- `artemis-preview-runner@.service` has no host Docker socket, protected policy,
  admission state or daemon credential. Its trusted controller receives the
  socket access group, not daemon authority. The PR build sandbox must not
  inherit that group, socket, workspace or controller credential. L5b1 owns the
  separate rootless builder and per-run output tree; this unit does not build.
- The existing `traefik.service` remains unchanged. It reads protected routes
  without a Docker socket and performs trusted pre-start recovery. See
  [preview-routing.md](preview-routing.md) for its separately pinned installation.

Both new units require `/etc/artemis-preview/activation.ready`. The marker is
**not** an approval, credential or security boundary. The reviewed daemon and
controller entrypoints do not exist in L5a1. The runtime owner must not create
the marker or enable these templates before the later layers, credential scope,
runner restrictions, human anchor and applicable cutover checks pass. Installing
or enabling units on live X99 is outside CHE-1429's scope.

## Protected layout

The runtime owner installs the reviewed code at
`/usr/local/libexec/artemis-preview/` (root-owned, not agent-writable), including
its protected Python environment. The daemon template uses that directory as
its working directory. `config/preview/tmpfiles.conf` describes these directories:

| Path | Owner / group | Mode and purpose |
| --- | --- | --- |
| `/etc/artemis-preview` | root / root | 0755; parent is traversable, not writable |
| `/etc/artemis-preview/credentials` | root / root | 0700; secret source, files 0600 |
| `/etc/artemis-preview/routes` | root / artemis-preview-ingress | 2750; readable route group; root-equivalent daemon publication |
| `/usr/local/libexec/artemis-preview` | root / root | 0755; reviewed control code |
| `/var/lib/artemis-preview` | artemis-previewd / artemis-previewd | 0700; daemon-only admission/seal/port state |
| `/run/artemis-preview` | artemis-previewd / artemis-preview-runner | 0750; future protected controller socket |

Install `policy.json`, `registry.json` and `scope.json` directly under
`/etc/artemis-preview`, owned by root with group `artemis-previewd` and mode 0640.
The daemon has ordinary read access, not ordinary file-write permission, to
the three documents. Docker makes the trusted daemon root-equivalent; filesystem
modes do not isolate policy from that trusted root authority. Their parent
and every ancestor must be root-owned and not group/world writable. Routes are
a separate writable surface; no policy lives in the file-provider directory.
Admission/seal records are daemon-owned, never runner-owned. The socket must
be 0660 with the dedicated controller group; no TCP admission listener is added.

The root-owned 2750 route tree preserves L4b1's layout. `ReadWritePaths` grants
mount-namespace access, not Unix file-write permission. Later daemon publication
must use reviewed privileged operations, including recovery/lock ownership,
without granting the ordinary runner or ingress identity write access. Direct
unprivileged publication into this tree is not implemented or claimed here.

`scripts.preview_control.load_control()` walks from the filesystem root with
held directory descriptors and `O_NOFOLLOW`. It rejects writable ancestors,
wrong owners, links, non-regular files, documents over 128 KiB, duplicate JSON
keys and unknown/missing fields. Reads are bounded and FIFO-safe. Returned
candidates and participant provenance are immutable. The alternate owner/anchor
arguments exist only for disposable fixtures; the CLI exposes no overrides.

## Policy and PR-to-issue registry

`config/preview/control.example.json` packages the three document shapes for
review. Its zero IDs, empty provenance and unverified scope intentionally fail
validation. It is not a provisioned policy. Split the documents only during
the runtime owner's separately authorized provisioning work.

Policy schema 1 pins repository name and native numeric ID, workspace/project,
revision, full protected controller SHA, same-origin trust scope, author and
reviewer agent IDs with model/lab, the separate daemon identity/credential
IDs, and `human_approver_ids`. The human allowlist contains 1–256 unique positive
numeric GitHub user IDs; strings and booleans fail validation. The runtime owner
records an ID only after confirming that no agent can access that account's
authentication or recovery paths on any host. Account names and native `User`
type do not establish custody. Old policies without this field fail closed.
Do not derive authorship from a shared GitHub login. The daemon identity
must not be an author or reviewer. L5a2 owns fetched native receipt/run mapping
and different-lab checks for **every** candidate author. L5a3 implements the
[exact-tuple human approval verifier](preview-github.md).

Registry schema 1 has `policy_revision` and at most 1000 `entries`. Each entry
has only `pr`, `issue_id`, `thread_id`, `head_sha`, `base_sha` and `author_ids`.
IDs are canonical nonzero UUIDs; SHAs are full lowercase nonzero commit SHAs.
PR numbers are positive integers below 2^63; booleans are not integers here.
Author IDs must be unique members of protected policy. Duplicate PR records,
unregistered PRs and mismatched revisions fail closed. The runtime owner records
verified issue/thread and source provenance; a submitter cannot supply them.
`Control.candidate(pr)` resolves only this protected tuple. It does not fetch
or accept a verdict, human review, caller path, digest or `approved` flag.
Policy updates require reviewed owner changes and a new revision. Re-fetching
native sources and invalidating admissions remain later-layer responsibilities.

## Platform credential scope check

`scope.json` is a **root-controlled owner assessment**, not a Multica response,
token introspection endpoint or authentication receipt. The scope field names
are this layer's normalized contract, not a claim about native permission names.
The platform/runtime owner must establish a supported, separately provisioned
identity and inspect its complete effective grants without exposing the secret.
Do not borrow an agent's login, create a second agent's signing identity or
infer read-only scope from a successful read. Preserve native administrative
evidence privately with the owner; no credential material belongs in the repo.

The assessment must match the policy's identity, credential, workspace and sole
project. Exactly `issue.read`, `comment.read`, `task.read` and `agent.read` are
allowed. These represent the native issue/comment/reviewer-run/agent metadata
reads required by L5a2. Wildcards, writes, administration, impersonation, missing
reads and duplicate grants fail. `complete`, `supported_identity` and
`exclusive_custody` must be true. Positive epoch-second `checked_at` and
`expires_at` must bracket the check time. L5a2 caps this window at 24 hours and
re-checks it before and after every native credential use. Reassess before expiry and whenever
custody or effective grants change. UTC epoch seconds are machine evidence,
not a human-facing time format.

If the platform cannot support the required identity, project restriction or
complete read-only grant evidence, keep the assessment unverified and activation
blocked. Root-owned assertions do not create unavailable platform capabilities.
This layer validates the owner assessment against disposable fixtures; live
identity provisioning and effective-grant verification are **NOT-RUN**. No
agent credential or runner environment was read for this implementation.

## Reproducible checks

```bash
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host/test_control.py -q
python3 -m scripts.preview_control check
```

The CLI reads only the fixed protected document paths and prints no secret or
document content. With no owner-provisioned documents it exits nonzero. A
successful check validates configuration only and reports admission NOT-RUN.
The unit tests exercise held-descriptor filesystem protections, candidate
binding, read-only scope negatives and immutable provenance. Native sysusers
dry-run and tmpfiles creation run only under a disposable root; fixture numeric
ownership does not establish real account isolation. Native systemd verification
uses disposable placeholder executables, not the not-yet-implemented entrypoints.
Missing native utilities skip their fixture checks explicitly. No live account, service, Docker daemon,
provider, device or Cloudflare resource is changed.
