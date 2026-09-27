# Jev Operator fast lane

The Operator can use Jev for routine next moves instead of invoking the frontier
vision model. This is independent of the Validator's `ARTEMIS_JEV_ENABLED`
safety-net flag. Both integrations reuse `artemis.services.jev.JevClient`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `ARTEMIS_JEV_FAST_LANE` | `off` | `off`, `shadow`, or `on` |
| `ARTEMIS_JEV_FAST_LANE_MODEL` | `jev-1.13` | Pinned model; other values are rejected |
| `ARTEMIS_JEV_FAST_LANE_THRESHOLD` | `0.9` | Minimum confidence; may be tightened up to 1 |
| `ARTEMIS_JEV_FAST_LANE_MAX_STREAK` | `3` | Consecutive Jev steps before frontier resumes; may be tightened to 1 or 2 |
| `TYPESAFE_API_KEY` | unset | Secret credential, required in `shadow` and `on` |
| `TYPESAFE_BASE_URL` | TypeSafe direct endpoint | Use `https://openrouter.ai/api/v1` for OpenRouter |

`off` makes no fast-lane network request. `shadow` starts Jev concurrently with
the frontier and never executes its answer. The request has a two-second total
budget; if the frontier finishes sooner, the unfinished shadow request is
cancelled and joined immediately rather than spending the remaining budget.
Thus shadow cannot add a Jev timeout to the frontier's critical path, and no
request survives the turn. Completed shadow answers still include confidence
and probabilities in their trace; a cancelled request records
`gate_reason=frontier_finished`.

`on` accepts only offered moves, at sufficient confidence, with labelled safe
tap targets. The English deny list blocks delete/remove/pay/buy/purchase/send/
submit/confirm/uninstall/reset/erase/sign-out/log-out. Icon-only targets and
labels with more than 20% non-ASCII letters escalate. The first scroll is always
chosen by the frontier; Jev may only repeat its direction. The consecutive-step
limit is derived from stored step metadata, not an Operator instance counter.
User instructions, stop requests, and recovery feedback stay with the frontier.

Moves use the real indexed-element indices. Launch moves come only from apps
named in the active milestone or leaf and already present in `package_cache`;
resolution is cached for that milestone. Every accepted move passes through
the ordinary tool translation/validation and the existing downstream safety
nets. Invalid tools, missing configuration, service errors, malformed answers,
timeouts, and oversized move lists fall back to the frontier in the same turn.

Fast-lane turns stage a synthetic assistant message and preserve the preceding
turn's step ID and validation result. They do not write plan notes and are
excluded from the frontier's unwritten-action ledger streak.

Every step records `extra_metadata.decision_source` (`jev` or `frontier`). A
`jev` trace named `fast_lane` records mode, choice, confidence, top-three
probabilities, gate result/reason, latency, whether the move was taken, move
count, launchable apps, and pinned model. Shared move/state/gate functions live
in `artemis/agents/operator/jev_fast_lane.py` for a future replay consumer;
this change does not implement replay.

## Data boundary

Enabling either `shadow` or `on` sends milestone text, foreground app, up to 60
visible text strings, and the last four executed actions to the configured
third-party endpoint. CHE-641 D1/D2 authorizes the fast-lane design. Cheese's
September 27, 2026 client-data decision additionally permits tevo-studio
client-app screen text for Jev `shadow`/`on` and replay, with no session-origin
filter. QA, agent, and Cheese sessions are not currently distinguishable; that
limitation is accepted by the recorded decision. The CHE-819 device evidence
scope remains Settings/Clock `shadow` on a leased X99 recyclable AVD only.
Default remains `off`; this implementation and shadow evidence do not qualify
`on` as a host default.
# Offline replay

Checkpoint verification can reopen completed milestones directly on disk, outside
the note traces. Replay interleaves timestamped failed milestone `verify` verdicts
with note operations to infer those possible resets. The ledger does not record
whether a failure actually reset the plan (stale results and exhausted repair
quotas may not), so affected steps remain `unverified`, with
`plan_reconstruction=inferred_checkpoint_reopen`, until a successful full
`task_plan` save records the live state. Failed checkpoint verdicts without a
usable timestamp conservatively mark the session's history as
`undated_checkpoint_verdict`. These steps retain their action truth, make no Jev
request, report `reason=unreconstructable_plan` in JSON, and appear in the table's
unreconstructable-step count; they never enter accuracy denominators.

`artemis jev replay --path ~/.artemis-traces-che645 --sessions dcf43ab9 --runs 3 --threshold 0.9 --json` evaluates recorded actions without controlling a device. Use `--all` instead of `--sessions` for the full X99 store. `--key-file` defaults to `~/.config/artemis/openrouter.key` (owner-only permissions); when using that file, replay uses OpenRouter unless `--base-url` or `TYPESAFE_BASE_URL` is set. `--json` includes per-step choices and gate reasons; without it, replay prints a summary table. The cost uses an estimated $0.00001 per call by default, overridable with `--cost-per-call`.

Only frontier decisions from PASS runs with completed plan milestones and passed milestone `verify` criteria in the independent `check_ledger.jsonl` are graded. A completed checkbox alone is not verification; missing, unchecked, failed, or inconclusive evidence leaves the milestone unverified. Other frontier actions are replayed but remain unverified; Jev decisions are excluded. Neither category enters the fast-lane denominator. Recorded text input, long presses, and other recognized frontier-only actions have `escalate` as truth, so an accepted routine move on those steps counts as confident-wrong. Ambiguous or unavailable action mappings remain ungraded.

Replay reconstructs the `task_plan` note at each step from successful note traces, using the same exact/relaxed/fuzzy matching as the live note tool, and reads the stored pre-action UI tree. It uses the same move, state, and gate functions as the live Operator. Replay is intentionally manual rather than a CI job; attach the JSON results when changing the prompt, move list, gate, or threshold. Older runs derive launchable apps from names in the goal when no `fast_lane` trace row exists. Empty `--sessions` selectors are rejected rather than selecting the whole store. `trace export/import` is deferred.
