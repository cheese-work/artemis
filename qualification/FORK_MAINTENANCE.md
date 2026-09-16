# Cheese Work fork maintenance contract

`cheese-work/artemis` tracks `google/artemis` (Apache-2.0, Pixel-Test-Engineering
"Fusion" team) under a **pinned-SHA policy**, not unpinned `main` tracking.
Upstream is early (repo created 2026-08-13, no tagged releases yet), so a
moving `main` is not a stable qualification target.

## What is pinned, and where

The single source of truth for the currently-qualified fork commit is
`qualification/manifests/pocket_actual_save_relaunch.v1.json`'s `fork_sha`
field (and any sibling manifest added for other qualification journeys).
CI, qualification runs, and rollout tooling read that field rather than
`origin/main` directly.

## Weekly upstream review

Once per week:

1. Fetch upstream (`git fetch google main` with `google` remoting to
   `https://github.com/google/artemis`, or the equivalent configured
   remote).
2. Diff `google/main` against the currently pinned `fork_sha`.
3. Triage: security fixes and bug fixes affecting the qualified journey(s)
   are candidates for promotion; unrelated feature work is not pulled in
   opportunistically.
4. Record the review (date, commits considered, promote/skip decision) as
   an issue comment on the fork-maintenance tracking issue (CHE-387) or a
   dedicated weekly-review issue if volume warrants it — not silently in
   git history alone.

## Manual pin promotion

Promoting the pinned SHA is manual and gated, never automatic:

1. Open a PR that bumps `fork_sha` (and any file that embeds it) to the
   new candidate commit.
2. The new SHA must pass the same independent-review chain as any other
   qualification input change (see `manifests/pocket_actual_save_relaunch.v1.json`'s
   `review_contract`): a changed `fork_sha` invalidates prior evidence per
   the revision policy, so promotion always implies a fresh qualification
   batch before the new pin is relied on for production rollout — not
   before the PR merges.
3. Carry-forward Cheese Work patches (this fork's own diffs from upstream)
   are rebase-checked against the candidate commit as part of the same PR;
   a promotion that breaks a carried patch is not mergeable as-is.

## Cheese Work patches vs upstream contributions

- Cheese Work-specific changes (Gate 1 manifest/reconciliation mechanism,
  qualification harness, this directory) live in the fork and are not
  proposed upstream by default.
- If a fix is generally applicable (not Cheese-specific), it may be
  proposed to `google/artemis` only through a Cheese-selected issue —
  never opportunistically from an unrelated task.
