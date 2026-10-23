# Tinos gated-interstitial demo journey

CHE-2016 device-free testcase candidate. This document defines one positive
scenario and one negative control. It does not run a device, install or launch
the app, or establish execution evidence.

## Pinned inputs

- Artemis runner: `feefd94c72dbce0fa1afc109ffe3d8254f565d6b`.
- Immutable testcase/manifest source: `096411113522abc37964256833fecbbac4778af4`, manifest version `9`.
- Candidate app: debug `adsTest` APK from `tevo-studio/sa049-dual-app` PR #145, head `3277551509acfdfa2615491a6b197cec741e4388`.
- APK SHA-256: `d5b3e52c40941dd0b9e473d04029d61ca64e9369e721e947eb2eef470791dc08`; size `67183120` bytes.
- Application ID: `com.dualix.dualmessenger.webscanner`. Use test ad IDs only. Do not rebuild or change the app build.

## Fixture and reset

Run only after two independent non-author reviews approve this exact candidate
and Sol grants an exclusive X99 AVD claim with an exact serial. The authored
case is for the X99 recyclable AVD lane; an executor must not choose or use a
device before that grant.

Before each positive or negative attempt:

1. Verify the installed Artemis runner matches the pinned runner revision.
2. Verify the APK SHA-256, byte size, package ID, and pinned app commit. Install
   only this APK after the required review and device grant. Do not substitute
   another build.
3. Use an isolated test AVD and a non-premium test state. Use no personal data.
   If the non-premium state or app identity cannot be confirmed, stop and
   report `NOT-RUN`; do not change account state to guess.
4. Force-stop the app, then cold-start it from the home screen. Each cold
   start resets Tinos show history. Do not clear app data; no data clear is
   needed for this fixture.
5. Confirm the home screen is ready and the test user is non-premium. Record a
   timestamped `TinosDemo` logcat baseline before the first feature click.

After each attempt, force-stop the app, confirm it is stopped, preserve the
recording and logcat, and return the exact granted AVD to Sol for verified
cleanup and release. Record cleanup even when an attempt fails. Do not reset
shared ADB or another owner's device.

## Positive scenario: allowed show, then gate denial

The test uses the normal home feature click for a non-premium user. It does not
call an ad API directly.

1. From the ready home screen, click the feature that triggers the interstitial
   once. Preserve the timestamped screen recording, the native action trace,
   and the `TinosDemo` logcat evidence.
2. Require the exact log `show allowed: shown and dismissed` for this click.
   Record any log line that starts with `revenue ` and verify that its
   placement is `main`. The allowed-show assertion does not pass without the
   expected allow log.
3. Click the same home feature a second time less than 20 seconds after the
   first click. Use native action timestamps to prove the interval.
4. Require the exact log `show denied by Tinos rules (NOT_ALLOWED)` for the
   second click. Tie the log to the second click by event order and timestamps.
5. Capture a final native Checker result and final capture in the same
   Artemis session. Preserve the raw timestamped `TinosDemo` logcat, recording,
   native action timestamps, Checker ledger, and final-capture identity.

The exact `not loaded; skipping` log or a `show failed (<code>)` log is neither
an allow nor a denial. If either appears instead of the required result, do
not count that click as an allowed show or a gate denial. Preserve the output
and report the attempt as failed or not-run according to the observed cause.

The app rule also limits shows to at most 3 shows per hour. This scenario
exercises the 20-second minimum interval only; it does not qualify or claim
coverage of the hourly cap.

## Negative control: deliberate first-click false assertion

Start from the same isolated cold-start fixture and capture the same evidence.
Perform exactly one normal home feature click. The false final assertion must
be this exact text:

> The first home feature click must be denied by Tinos rules (NOT_ALLOWED).

The actual first-click evidence must show `show allowed: shown and dismissed`
and a `revenue ` event for placement `main`. The negative control is valid
only when the same-session final Checker assertion fails specifically because
the first-click denial assertion is false. Record that result as the evidence
schema verdict `fail_assertion` only when the runner-bound final capture is
linked to that Checker trace. Preserve the failed assertion, Checker trace
identity, linked final-capture identity, click trace, and raw `TinosDemo`
logcat. An orchestration status such as `completed` is not sufficient.

If the first click logs `not loaded; skipping`, `show failed (<code>)`, or no
allow event, the negative control is invalid; it did not fail for the intended
reason. A pass, generic Checker failure, stale or mismatched capture, missing
logcat, or missing same-session linkage is also an invalid control. Do not use
`qualification/tools/checker_verdict_exit.py` unchanged: its oracle is
Pocket Actual-specific. Apply the Tinos event assertions above to the raw
Tinos evidence and the pinned runner's same-session Checker records.

## Execution and reporting

Use the pinned runner's direct CLI entry point in foreground with a unique
session ID for each attempt. Set `ARTEMIS_TRACES_DIR=<ledger-root>` and
`DATA_ENGINE_DB_PATH=<ledger-root>/data_engine.db` before launching the
standalone CLI. Use `artemis run --standalone --session-id <session-id>` and
the app/goal arguments supported by that pinned runner revision. Do not use an
asynchronous run-and-yield flow. A reviewer or executor must confirm the
remaining CLI arguments from the pinned runner before execution; this
candidate does not guess at flags that vary by runner revision.

For each attempt, record the exact command, runner SHA, candidate commit SHA,
manifest version, APK identity and digest, target class, Sol-granted serial,
session ID, attempt kind, timestamps, Checker verdict, same-session final
capture identity, raw logs, recording, cleanup result, and observed cost. Use
`qualification/evidence_schema.v1.json` version `1` for per-attempt records;
keep its verdict separate from the issue-level result. Attach the raw logcat,
recording, native traces, Checker ledger, and cleanup/release evidence to the
execution report. Label all uncollected evidence `NOT-RUN`; never infer a pass
from an orchestration result.

No device execution, qualification, revenue collection, or app publication
occurred during testcase authoring. Two independent reviews and Sol's exact
serial grant are still required before any execution. Sol's corrected-build
re-review on CHE-2015 is also required before execution. CHE-387's flake
baseline is a separate required-acceptance gate and is not waived by this
candidate.
