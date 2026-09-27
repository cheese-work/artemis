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
third-party endpoint. Local recording is not consent to transmit client data.
CHE-641 D1/D2 authorizes Cheese Work app runs, including Settings/Clock fixtures
on a leased X99 recyclable AVD. Do not enable this feature on tevo-studio
client-app sessions or replay those sessions until Cheese records the separate
client-data-policy decision. Default remains `off`; this implementation and
shadow evidence do not qualify `on` as a host default.
