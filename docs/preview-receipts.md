# Native receipt verification and pending admissions (L5a2)

This layer adds `scripts.preview_receipts`. It reads native Multica metadata,
verifies the independent reviewer receipt and stores daemon-private records.
It does not install a service, provision credentials, dispatch a build or
publish a route. **This module alone cannot admit a preview.** The
[L5a3 human GitHub anchor](preview-github.md) remains mandatory even when every
check in this layer passes.

The protected configuration and PR-to-issue registry come from
[preview-control.md](preview-control.md). The submitter supplies a numeric PR
and verdict UUID, never an issue, thread, author, lab, path or approval flag.
Only the daemon loads protected configuration. `CandidateState` is an internal
input from the daemon's fresh native GitHub candidate/required-CI check, not
submitter data or an API request schema. L5a3 supplies that integration with
its separately fetched human review. An absent, pending or failed required-CI
result must set `ci_passed=False`. Shared GitHub login names do not establish
candidate authorship; the protected source/author registry does.

## Native reads

`MulticaReader` uses argv-only calls with separate stdout and stderr, a
30-second timeout, a 2 MiB stdout bound and a 128 KiB stderr bound:

```text
multica issue get <protected-issue> --output json
multica issue comment list <protected-issue> --thread <protected-root> --tail 30 --output json
multica issue runs <protected-issue> --output json
```

The protected scope assessment window is at most 24 hours. The reader checks
`checked_at <= now < expires_at` before and after **each** CLI call, including
every pagination request. Expiry during a call fails the read and prevents
another credential use. Admission preparation also checks this window.

Comment reads do not use `--compact`, `--summary` or folded reads. The reader
follows both `--before` and `--before-id` from each native reply cursor, even
after finding the target verdict. It reads the complete protected thread to
detect newer reviewer activity. Each page has at most 31 comments; the limit
is 100 pages. Exhaustion, malformed/repeated cursors, duplicate/changing
comments, an absent root, broken ancestry or incomplete run history fails
closed. Oversized histories require an owner-reviewed new bounded review
thread, not an unbounded read or a caller-selected substitute.

The native issue must match protected workspace/project. The verdict's native
author must be an allowlisted agent reviewer. Its `source_task_id` must map to
one completed issue/workspace run, with the same agent and native dispatch
comment as the verdict's direct parent. That dispatch can be nested under the
protected root; complete ancestry checks keep it inside the protected thread.
A sibling dispatch cannot supply the verdict's run. The run must list its exact
incoming dispatch comment in `delivered_comment_ids`. This native field records
comments delivered to the run, not verdict comments posted by it. The outgoing
verdict binds the run through native `source_task_id`, not that incoming list.
Verdict creation and update must fall inside the completed run. Missing native
revision, update time or attribution fails; the body cannot supply them.
Every native comment author ID is type-checked as a canonical UUID before
allowlist membership. Malformed IDs invalidate rather than raising TypeError.

Model and lab provenance comes from the root-controlled participant registry,
not a self-assertion or a shared bot identity. Native runs do not expose a
historical model/lab snapshot in the observed CLI contract. The runtime owner
must pin that provenance when recording the candidate and reviewer dispatch.
The reviewer must be independent and from a different lab than **every**
candidate author. A native matching run is attribution evidence, not a second
trust anchor: shared-UID platform credentials still permit forgery. L5a3's
agent-inaccessible human GitHub approval addresses that separate decision.

Lab comparison uses Unicode NFKC normalization and case folding. The normalized
key must contain only ASCII letters, digits, dots and hyphens. Full-width/case
variants therefore cannot claim diversity; remaining non-ASCII homoglyphs fail
closed. The runtime owner must use one canonical key for each actual lab, not
different ASCII aliases for the same provider.

## Machine-readable receipt

The complete comment body is one UTF-8 JSON object. Prose containing PASS or
a SHA, Markdown fences, duplicate/unknown fields and partial acknowledgements
are not receipts. The reviewer publishes the object under their own native
identity. Schema 1 has exactly these fields:

```json
{
  "schema_version": 1,
  "decision": "PASS",
  "repository": "cheese-work/artemis",
  "repository_id": 1365625873,
  "pr": 70,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "author_ids": ["11111111-1111-4111-8111-111111111111"],
  "reviewer_id": "22222222-2222-4222-8222-222222222222",
  "reviewer_model": "reviewer-model",
  "reviewer_lab": "reviewer-lab",
  "policy_revision": 1,
  "same_origin_scope": "trusted-root-origin-insiders",
  "scope": "path-preview-admission",
  "complete": true,
  "unresolved_p0_p1": 0
}
```

These are disposable example values, not a review of PR 70. Each tuple and
participant field must equal protected configuration. No scoped PASS, unresolved
P0/P1 finding or missing author provenance qualifies. Numeric booleans fail.
Author IDs retain the protected registry order. Newer allowlisted reviewer
activity, including edits to older comments, requires a fresh receipt. This is
conservative: even a newer PASS or non-verdict reviewer note invalidates the
earlier record rather than guessing whether that activity is harmless.

## Record lifecycle

`prepare()` creates an immutable `pending-human` record. It retains comment ID,
native revision, exact UTF-8 content SHA-256, creation/update timestamps,
`source_task_id`, native reviewer ID/model/lab, candidate authors, repository/PR,
full head/base SHAs, protected issue/thread and policy revision. The record also
binds a canonical hash of the complete loaded control/provenance snapshot.
Expiry is the earlier of 24 hours from preparation and credential-scope expiry.

`revalidate()` re-fetches native evidence and checks the fresh candidate state.
Changed/deleted content, revision or attribution, newer reviewer activity,
changed head/base/policy/provenance, failed CI, a closed PR, API failure or
expiry irreversibly invalidates that record. A refresh never extends its TTL.
An invalidated record never revives. Revalidation returns the updated record;
the daemon must persist an invalidation with `AdmissionStore.invalidate()`.
The protected store accepts `pending-human`, `admitted` and `invalidated` states.
Only L5a3 creates an `admitted` record after verifying both native anchors.
`require_admitted()` rejects pending, invalidated, expired and structurally
incomplete records. It is a local guard, not a remote evidence refresh. A later
daemon must load its own protected record and call L5a3 `revalidate()` before
each use; submitters cannot supply record objects or status flags.

The runtime owner provisions `/var/lib/artemis-preview/admissions` with daemon
ownership and mode 0700. `AdmissionStore` walks protected ancestors through
held descriptors, rejects symlinks/writable ancestors and reads only bounded,
single-link, daemon-owned 0600 regular files. IDs derive filenames. Creation
uses a synced temporary file and atomic create-once link. Invalidation uses a
synced atomic replacement. It changes no unrelated record and leaves no scratch
on handled errors. Fixture owner/anchor/path overrides are not a daemon request
interface. No live directory is created by this layer.

L5a3 extends admission only after independently verifying and binding the
human approval. Later daemon integration must re-fetch both anchors before
build dispatch and publication, reconcile every five minutes, withdraw failed
revalidations and enforce TTL locally during API outages. This layer provides
record checks, not a running reconciliation timer or route-withdrawal service.
No live platform credential scope, human custody or native GitHub admission
integration is claimed. Do not test this reader with borrowed agent credentials.

## Checks

```bash
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host/test_receipts.py -q
python3 -m pytest --confcutdir=tests/unit/preview_host tests/unit/preview_host -q
```

Tests use disposable CLI responses and files. They make no provider, device,
credential, live host-control or deployment calls.
