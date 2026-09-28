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
4. Confirm the entry screen is actually reachable and usable, not showing a
   secure-storage blocking heading ("Secure storage unavailable" / "Your
   existing recovery data was not opened..."). The candidate's entire entry
   create/save path runs through an Android Keystore-backed encrypted store
   (`MainActivity` gates all content behind `AndroidEncryptedRecoverySnapshotStore`
   load success); there is no independent, unencrypted persistence path. A
   device or AVD with a degraded Keystore (no configured lock screen, no
   StrongBox, key generation requiring user authentication that was never
   set up) will show this screen instead of the entry UI, on every attempt,
   with no crash and no disconnect. This step catches that before the
   attempt is budgeted: if the blocking heading is showing after cold
   start, the attempt is `fail_infrastructure` or `device_unavailable` (per
   verdict discipline below), is recorded visibly, and does not count
   toward the tier's 10 positives — the same treatment a missing/offline
   device already gets. It must never be scored `fail_assertion`, since an
   attempt that starts this way cannot create an entry to fail an assertion
   about. This is a scripted ADB check, not an Artemis-driven step, and
   runs outside any budgeted attempt.

`<candidate_package>` and `<device_serial>` are supplied at run time by the
execution stage (CHE-540+); this journey does not pin them.

## Pinned app workflow

This candidate is Pocket Actual's recovery flow, not a generic label/value
entry form. The attempt must use these app controls and record the listed
evidence:

1. Tap **Build current picture** and reach `current-picture-setup`.
2. Tap **Add Account**, enter account name `qual-<run_id>`, choose
   **USD - US dollar**, and save with `save-account-action`.
3. At `verified-balance`, enter the signed two-decimal value `+123.45` in
   `verified-balance-amount-input`, then tap
   **Confirm Verified balance**.
4. Reach `recovery-snapshot` and record `qual-<run_id>`,
   `USD · +123.45`, **History gap**, and **Recovery adjustment**.

## Positive run — natural language task instructions

Goal text passed as `task_desc` (MCP `mobile_run_task`) / `goal` (CLI `artemis run`):

> Open the app. Build the current picture. Add an Account named
> "qual-<run_id>", choose USD - US dollar, and record the valid decimal
> "+123.45" as its Verified balance. Reach the Recovery snapshot and
> confirm its account, USD balance, History gap, and Recovery adjustment
> markers. The execution controller must then record a successful
> `stop_app` / `am force-stop` for the candidate package, verify the saved
> process ended, and only then relaunch the app from the home screen. Open
> the Recovery snapshot and read back the same account, USD balance, History
> gap, and Recovery adjustment markers.

`expected_output_desc` / `output_description`:

> Report the exact post-relaunch `qual-<run_id>` account name and
> `USD · +123.45` balance, plus the History gap and Recovery
> adjustment markers, with the process-stop and process-end records.

### Assertions (positive run)

1. **Save succeeded**: the recovery snapshot shows `qual-<run_id>`,
   `USD · +123.45`, **History gap**, and **Recovery adjustment**
   before the stop/relaunch interval begins.
2. **Process termination is proven**: after UI save and before relaunch,
   the execution record shows a successful `stop_app` / `am force-stop`
   and a process check that the saved process ended. Without both records,
   the attempt is invalid and cannot pass.
3. **Relaunch reached the same screen**: after relaunch, the app reaches the
   Recovery snapshot — this is graded as part of the run, not scripted,
   since the navigation path is exactly what a real user would need and is
   part of what qualification is proving.
4. **Persisted state matches**: the post-relaunch account name and USD
   balance equal `qual-<run_id>` and `USD · +123.45` byte-for-byte,
   and **History gap** plus **Recovery adjustment** remain visible. Anything
   else (empty, default, a different prior value, an app crash) is
   `fail_assertion`.
5. **Verdict discipline**: an attempt that never reaches a relaunched,
   readable state (app crash, ANR, device disconnect, ADB failure) is
   `fail_infrastructure` or `device_unavailable`, never silently folded
   into `fail_assertion` — see `evidence_schema.v1.json`'s `verdict` enum.

## Negative control — deliberately false assertion

Same fixture, same save step, but the checker is told to verify an account
name that provenance guarantees is wrong:

> ...(identical save/relaunch steps as the positive run)... Open the same
> Recovery snapshot and confirm its account name reads
> "qual-<run_id>-WRONG-SUFFIX" exactly.

Count this control only when its record proves all of the following: the UI
saved the original `qual-<run_id>` account and `USD · +123.45` balance, the
post-save process stop succeeded and the process ended, the app relaunched,
the post-relaunch account name was observed, and that observed name was
compared with "qual-<run_id>-WRONG-SUFFIX". The negative task must state this
as its final assertion verbatim: `The post-relaunch account name must equal
qual-<run_id>-WRONG-SUFFIX.` The executor must run with a pinned
`--traces-path` and `--session-id`, then run:

```bash
python qualification/tools/checker_verdict_exit.py \
  --ledger <traces-path>/<session-id>/check_ledger.jsonl \
  --observed-account qual-<run_id> \
  --expected-account qual-<run_id>-WRONG-SUFFIX
```

The adapter reads the append-only final Checker ledger record, writes a
machine-readable verdict, and exits `1` only when that exact final `assert`
failed. It exits `2` for missing, malformed, unrelated, or non-failed
evidence; only exit `1` is control success. Record the adapter's JSON output,
ledger path, and exit code with the observed/expected account evidence. An
orchestration status such as `completed` is not control success. A missing
prerequisite, generic checker failure, zero adapter exit, exit `2`, or `pass`
is an invalid control and unqualifies the entire batch. Record it as its own
evidence entry (`attempt_kind: "negative_control"`), not folded into the N=10
positive count (CHE-388 plan v2: "freeze N=10 positive runs per target/tier
plus one separate negative control").

## Bounded execution and cleanup

- One attempt = one `mobile_run_task` / `artemis run` invocation from a
  freshly reset fixture to either a verdict or a hard timeout.
- After every attempt (pass, fail, or infrastructure failure), re-run the
  fixture/reset steps and confirm cold-start state before releasing the
  device lease — this is `cleanup_verified` in the evidence schema.
- No attempt escalates tier, retries silently, or mutates the manifest
  mid-batch. A revised candidate or manifest requires a fresh batch (see
  `manifests/pocket_actual_save_relaunch.v1.json`'s `revision_policy`).
