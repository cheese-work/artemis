# Qualification journey: pocket-actual save/relaunch

CHE-537 source packet. This is the smallest retrievable journey for CHE-388
qualification: persist data via normal UI interaction, kill and relaunch the
app, and assert the persisted state survived — plus one deliberately false
assertion to prove the pipeline can fail for the right reason.

This file is instructions and assertions only. It does not run anything, is
not evidence of a pilot pass, and does not implement Gate 1's manifest or
reconciliation mechanism (that mechanism is PR #1 / CHE-491, referenced by
digest in `manifests/pocket_actual_save_relaunch.v1.json`, and reused, not
rebuilt, here).

## Fixture / reset

Before every attempt (positive or negative control):

1. Force-stop the candidate app: `adb -s <device_serial> shell am force-stop <candidate_package>`.
2. Clear app data: `adb -s <device_serial> shell pm clear <candidate_package>`.
3. Confirm cold-start state: relaunch once, verify the entry screen shows no
   prior data (empty/default state). This is a scripted ADB check, not an
   Artemis-driven step, and runs outside any budgeted attempt.

`<candidate_package>` and `<device_serial>` are supplied at run time by the
execution stage (CHE-540+); this journey does not pin them.

## Positive run — natural language task instructions

Goal text passed as `task_desc` (MCP `mobile_run_task`) / `goal` (CLI `artemis run`):

> Open the app. Create a new entry with the label "qual-<run_id>" and the
> value "<random_token>" (substitute the actual run id and a freshly
> generated random token before dispatch — never reuse a token across
> attempts, so a stale relaunch can never be mistaken for a fresh save).
> Save it. Force the app to the background, then fully close it. Relaunch
> the app from the home screen. Open the same entry and read back its
> value.

`expected_output_desc` / `output_description`:

> Report the exact value read back for the entry labeled "qual-<run_id>"
> after relaunch, and whether it matches "<random_token>" exactly.

### Assertions (positive run)

1. **Save succeeded**: the app's own UI confirms the entry was created
   (e.g. a visible list item, a save confirmation) before the
   background/close step begins.
2. **Relaunch reached the same screen**: after relaunch, the agent
   navigates back to the entry (via whatever path the app's UI requires) —
   this is graded as part of the run, not scripted, since the navigation
   path is exactly what a real user would need and is part of what
   qualification is proving.
3. **Persisted value matches**: the value read back after relaunch equals
   "<random_token>" byte-for-byte. Anything else (empty, default, a
   different prior value, an app crash) is `fail_assertion`.
4. **Verdict discipline**: an attempt that never reaches a relaunched,
   readable state (app crash, ANR, device disconnect, ADB failure) is
   `fail_infrastructure` or `device_unavailable`, never silently folded
   into `fail_assertion` — see `evidence_schema.v1.json`'s `verdict` enum.

## Negative control — deliberately false assertion

Same fixture, same save step, but the checker is told to verify a value
that provenance guarantees is wrong:

> ...(identical save/relaunch steps as the positive run)... Open the same
> entry and confirm its value reads "<random_token>-WRONG-SUFFIX" exactly.

This must produce `fail_assertion` (Checker or Outputter catches the
mismatch and reports failure) on every run. A negative control that comes
back `pass` invalidates the entire batch it belongs to — the pipeline is
not distinguishing true from false, so no positive result in that batch
can be trusted either. Record it as its own evidence entry
(`attempt_kind: "negative_control"`), not folded into the N=10 positive
count (CHE-388 plan v2: "freeze N=10 positive runs per target/tier plus
one separate negative control").

## Bounded execution and cleanup

- One attempt = one `mobile_run_task` / `artemis run` invocation from a
  freshly reset fixture to either a verdict or a hard timeout.
- After every attempt (pass, fail, or infrastructure failure), re-run the
  fixture/reset steps and confirm cold-start state before releasing the
  device lease — this is `cleanup_verified` in the evidence schema.
- No attempt escalates tier, retries silently, or mutates the manifest
  mid-batch. A revised candidate or manifest requires a fresh batch (see
  `manifests/pocket_actual_save_relaunch.v1.json`'s `revision_policy`).
