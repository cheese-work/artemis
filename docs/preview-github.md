# Human GitHub approval anchor (L5a3)

`scripts.preview_github` supplies the second admission anchor. It reads GitHub
and Multica evidence; it does not submit reviews, install a daemon, register a
runner, dispatch a build, import an image or publish a route. Operational
activation remains gated on the later reviewed controller/runtime layers.
Human-account custody and daemon credential provisioning are NOT-RUN here.

## Protected identity and native reads

The root-controlled `policy.json` has a nonempty numeric `human_approver_ids`
allowlist. The runtime owner adds an account only after the human confirms
exclusive custody. Tokens, browser/keychain sessions, recovery paths and
delegated automation must be inaccessible to every agent on every host.
Neither a login string nor native GitHub `User` type proves that property.
This code never inspects account credentials or signs in as the approver.

The submitter supplies only a numeric PR, native Multica verdict UUID and
numeric GitHub review ID. The daemon derives issue/thread and authorship from
its protected registry. `GitHubReader` executes argv-only `gh api graphql`
reads using the daemon's separately provisioned read credential. The PR and
review bodies are fetched by the reader, never supplied by the submitter.

Each call has a 30-second timeout, 2 MiB stdout bound and 128 KiB stderr bound.
The protected scope window is checked before and after every credential use.
The reader fetches all review pages, 100 reviews per page and at most 100
pages. It rejects GraphQL partial errors, incomplete/deleted results,
duplicate reviews, changed totals, repeated/missing cursors and exhausted
pagination. History beyond that bound needs an owner-reviewed policy/reader
change, not a submitter-selected shorter history.

The native repository ID/name, PR number and full head/base SHAs must match
the protected candidate. The PR must be open and same-repository, not a fork.
The exact head commit must have a native status-check rollup of `SUCCESS`;
an absent rollup never proves CI passed. The native `mergeStateStatus` must
also be `CLEAN` (mergeable with passing commit status). This intentionally
also refuses `BEHIND`, `HAS_HOOKS`, `BLOCKED`,
`UNSTABLE`, `DIRTY` and `UNKNOWN`; it never guesses that pending, missing or
failed required checks passed. No caller supplies `ci_passed` to this layer.
The existing Multica checks still enforce every candidate author's independent
different-lab reviewer, complete PASS, scope and zero unresolved P0/P1.

## Exact approval body

The human reviews the candidate and the independent evidence in their own
session, then submits an `APPROVED` GitHub PR review. A generic merge approval
or an automated copy of a PASS is insufficient. The complete review body is
one JSON object with exactly this schema; values below are disposable examples,
not an approved candidate or provisioned account:

```json
{
  "schema_version": 1,
  "decision": "APPROVED",
  "repository": "cheese-work/artemis",
  "repository_id": 1365625873,
  "pr": 70,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "verdict_id": "77777777-7777-4777-8777-777777777777",
  "verdict_revision": 1,
  "verdict_content_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "source_task_id": "88888888-8888-4888-8888-888888888888",
  "author_ids": ["11111111-1111-4111-8111-111111111111"],
  "reviewer_id": "22222222-2222-4222-8222-222222222222",
  "reviewer_model": "reviewer-model",
  "reviewer_lab": "reviewer-lab",
  "policy_revision": 1,
  "same_origin_scope": "trusted-root-origin-insiders",
  "scope": "path-preview-admission",
  "complete": true
}
```

All values and their types must exactly match the newly fetched receipt and
protected tuple. Prose, fences, duplicate/unknown keys and booleans substituted
for numeric IDs/revisions fail closed. Author IDs retain the protected order.
The native review ID and native approver ID are stored separately from the body.

The selected review must currently be `APPROVED`, authored by an allowlisted
native `User`, and attached to the exact head commit. Bot, shared/unallowlisted,
pending, dismissed, deleted and wrong-head approvals are refused. GraphQL
`lastEditedAt` must be present and null: even an edited-then-restored approval
cannot be reused. Native creation/submission/update must be ordered, no earlier
than the reviewer receipt and no later than the admission check. Newer blocking
reviews or a newer submitted review by the same approver require a fresh human
approval. Reviews on later pages receive the same checks.

## Admission and invalidation

`admit()` brackets the native Multica receipt read with fresh GitHub snapshots.
The second snapshot refuses approval or candidate changes during that read. It
creates an immutable `admitted` record with both anchors, policy fingerprint,
review ID, numeric approver ID, content hash, commit and native timestamps.
The existing daemon-private create-once `AdmissionStore` persists the record;
the submitter cannot write the store. A `pending-human` record is never promoted
by a supplied flag. Fresh admission creates a new protected record.
Legacy records without the `approval` field fail closed and must be recreated
from current evidence; no existing record is silently promoted.

`revalidate()` re-fetches both anchors and the current candidate. It preserves
the existing record ID and expiry only when both receipts are unchanged.
Policy/allowlist changes, edited/dismissed/deleted approval, changed verdict,
new blocking review, head/base change, non-clean CI and API failure return an
`invalidated` record. Invalidated records cannot be revived. New tuples require
a new human approval and a new admission.

The daemon must persist returned invalidations and revalidate before build
dispatch, before publication and during reconciliation. API failures allow no
new build or route. Later reconciliation must withdraw an existing preview at
the next 5-minute pass; its local timer must enforce the hard 24-hour TTL during
API outages. This layer supplies the verdict, not that timer or route mutation.
Remote APIs do not form a transaction; revalidation at every privileged use
remains mandatory. The library is internal to the protected daemon, not a
submitter-facing record schema.

## Disposable checks

```text
PYTHONDONTWRITEBYTECODE=1 pytest tests/unit/preview_host -q -o addopts= --confcutdir=tests/unit/preview_host -p no:cacheprovider
```

These tests use injected native CLI responses and private temporary stores.
They cover exact binding, numeric allowlisting, bot/edited/dismissed/stale
reviews, pagination, required-CI refusal, both-anchor invalidation, expiry,
malformed records and API failure. No provider, device or privileged host call
is needed. Live human custody, authenticated approval enforcement and deployment
remain separate acceptance evidence; unit PASS does not establish them.
