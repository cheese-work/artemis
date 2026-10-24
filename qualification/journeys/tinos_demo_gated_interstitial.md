# Tinos gated-interstitial demo journey

CHE-2016 device-free testcase candidate. This journey defines one allowed show,
one 20-second gate denial, and a separate intentional negative control. No
device has been used to author this candidate.

## Pinned inputs

- Artemis runner: `feefd94c72dbce0fa1afc109ffe3d8254f565d6b`.
- Testcase and manifest format reference: `096411113522abc37964256833fecbbac4778af4`, manifest version `9`. Pocket Actual files are format references only; the Tinos case uses its own manifest and assertions.
- Candidate Tinos manifest: `qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json`.
- App: current SDK-main debug `adsTest` APK from `tevo-studio/sa049-dual-app` PR #145, head `24d3ad51350767d04a2951ab72685a45f67a6f75`.
- APK SHA-256: `b2a14979f173bc3e022a15aa5e3385085c8a02167ceda4b64b69da9e060efe64` (preserved-copy observation matches the author-reported digest; Sol has not independently hashed this APK).
- APK size: `67183124` bytes (observed on the preserved copy; verify the APK SHA-256 before use; do not use the superseded stack-head APK or the APKs in `workdir/out/` and `workdir/out2/`).
- Application ID: `com.dualix.dualmessenger.webscanner`. Use test ad IDs only. Do not rebuild or change the app build.
- Google test ad app ID: `ca-app-pub-3940256099942544~3347511713`.
- Tinos fixture: placement `main`, minimum interval 20 seconds, maximum 3 shows per hour. A cold start resets show history.
- Test interstitial unit: `ca-app-pub-3940256099942544/1033173712`.
- Do not use the older `b219d55` APK.
- PR #100's merge is not the runner pin.

The `TinosDemo` log tag emits the exact allow line
`show allowed: shown and dismissed` and the exact denial line
`show denied by Tinos rules (NOT_ALLOWED)`. A revenue line has the format
`revenue <valueMicros> micros <currency> unit=<adUnitId> network=<network> precision=<precision>`.
The revenue line has no placement field. For this fixture, require
`unit=ca-app-pub-3940256099942544/1033173712`; `main` is the configured Tinos
placement, not a revenue-log field.

## Fixture and reset

Execution requires two independent non-author reviews of this exact revision.
The signed app-source PASS on CHE-2015 remains applicable because the main
build changes only the SDK pin and repo-local Maven artifacts. It does not
verify this APK's bytes. The executor must verify the author-reported APK
SHA-256 before use; earlier binary evidence does not transfer to this build.
Sol must grant an exclusive X99 AVD claim with an exact serial. The X99 AVD
must follow its live qualification or first-run route. CHE-387's flake
baseline remains a separate required-acceptance gate.

1. Verify the installed Artemis runner against the pinned SHA. Verify the APK
   SHA-256, package ID, and app commit. Record its observed byte size. Install
   only this APK after all execution gates pass. Do not substitute or rebuild
   the app.
2. Use an isolated test AVD with no personal data and a dedicated non-premium
   test state. On the first cold start, expect the Splash/UMP consent gate. If
   first-run flags require them, complete Language First-Open, Privacy Ack,
   Forced Re-Login, and Onboarding in that order before Home. Do not enter
   personal credentials or invent account state. If a required screen needs
   personal data, or the app cannot reach Home in the test state, report
   `NOT-RUN`.
3. Before each independent case, force-stop and cold-start the app. Do not
   clear app data. This cold start resets Tinos show history. Repeat any
   required first-run screen only if the app presents it again. Do not
   cold-start between the two clicks in the positive case.
4. For a non-premium user, the Premium paywall auto-opens about one second
   after Home appears. Dismiss that paywall with Android system Back, without
   purchasing, and confirm Home is visible again. If the paywall does not close
   or Home does not return, report `NOT-RUN`.
5. At Home, confirm the `DiscountBadge` is visible. The app renders this cue
   only when `rememberIsPremium()` is false. If the cue is absent or Premium is
   open, report `NOT-RUN`; do not change subscription or account state to guess.
6. Select the visible label "WhatsApp". Confirm the Home UI tree shows the
   selected tab and visible label "Direct Message". If either label is missing,
   report `NOT-RUN`; do not guess. Do not use Compose test tags or hidden
   resource IDs; Home does not expose those tags as UI-tree selectors.
7. Use a new, empty attempt root. Start logcat capture with the manifest's
   `log_capture_argv_template` before starting Artemis. The testcase-side
   `qualification/tools/capture_tinos_demo_logs.sh` records a device epoch
   cursor from `adb -s <Sol-granted-exact-serial> shell date +%s`, bracketed by
   host epoch samples, then runs exactly
   `adb -s <Sol-granted-exact-serial> logcat -v epoch -T <start-cursor> -s TinosDemo:I '*:S'`.
   It records the exact logger PID and cursor in
   `<attempt-root>/tinos-demo-logcat.meta`. Its exit, interrupt, and termination
   traps stop and wait for that PID after Artemis returns or on another outcome.
   It records both exit statuses. SIGKILL can bypass the wrapper's EXIT trap.
   After every outcome, confirm the logger PID is gone before
   device release. If the PID remains, follow Sol's granted cleanup route and
   do not release the device until the logger is gone. Do not run a separate
   logcat process, clear shared ADB or log buffers, or reuse either capture file.
8. Capture the timestamped screen recording and native action trace in the same
   Artemis session. Use native step timestamps as the host epoch timeline. Map
   each device log timestamp to that timeline with the recorded host/device
   clock samples; retain the offset and uncertainty. The verifier rejects a
   recognized log event unless its full uncertainty interval is inside that
   session's native step window and strictly after the first feature tap. It
   also rejects a cursor that may begin after the first tap. Normalize
   recording timestamps to the host epoch timeline. The verifier reads exactly
   two feature-tap timestamps from this session's `steps` rows for positive
   mode and exactly one for control, matching action `tap` with
   `target_text` exactly `Direct Message`. Do not enter tap timestamps
   manually. For positive mode, the executor reads `first_visible_at` and
   `closed_at` from the recording; these are the only action-time verifier
   inputs.
9. For each shown interstitial, close it with its visible close control within
   15 seconds of the first visible frame. Record show and close timestamps from
   the recording. If the ad is not closed within 15 seconds, report that
   attempt `NOT-RUN`; the allow line is emitted only after dismissal. The app's
   15-second show wait starts when it calls `show`, not when the first frame is
   visible. For either case, if the allow line is absent after a close within
   15 seconds, report that attempt `NOT-RUN` for the timeout race. Do not count
   the missing line as a denial or a pass.

After each attempt, stop the app, preserve all raw evidence, and return the
exact granted AVD to Sol for verified cleanup and release. Record cleanup on
success and failure. Never reset shared ADB or touch another owner's device.

## Positive case: allow, then deny

Case ID: `tinos-demo-allow-then-interval-denial`.

This case uses ordinary Home UI actions for the non-premium test state. It does
not call the ad API directly.

1. At Home, confirm visible label "WhatsApp" and visible label "Direct Message"
   in the UI tree. Tap the visible label "Direct Message". Preserve the native
   action trace and timestamped recording; the matching step must record
   `action: tap` and `target_text: Direct Message`.
2. Close the displayed ad with its close control within 15 seconds of its first
   visible frame. Record the show and close timestamps from the recording and
   calculate `D` as the visible duration in seconds. If the ad is not closed
   within 15 seconds, report this attempt `NOT-RUN`. After dismissal, require
   `show allowed: shown and dismissed` for this tap. The app then navigates to
   Direct Message. Require at least one revenue line for this first tap that
   matches the complete format above, including the literal `micros` token
   and exact test-unit `unit=` value.
   Evaluate this revenue requirement from the continuous logcat capture; do
   not delay the repeat tap to wait for it.
3. Return to Home exactly once with Android system Back, not the app
   toolbar/back control. `NavDisplay` handles system Back by popping the route;
   the screen's own back callback invokes the separate `inter_back` ad path.
   Confirm Home, the visible "WhatsApp" tab label, and the same "Direct
   Message" row before tapping again.
4. Measure `t1` at the first exact allow-log timestamp after dismissal, mapped
   to the host epoch timeline from the capture metadata. The verifier derives
   `t2` from the second matching tap row in this same session after step 3; do
   not enter either tap timestamp manually. Normalize action and recording timestamps to that host epoch timeline and record
   their offsets. Let `D` be the first ad's visible
   duration in seconds, from its first visible frame to close-control
   activation in the recording. The SDK records show history when the ad is
   shown, not dismissal, and checks the second show only after loading, which
   can take up to 4 seconds. Include the 1-second timestamp uncertainty in
   the required bound: `(t2 - t1) + D + 5 seconds <= 20 seconds`. Derive the
   tap window as `t2 - t1 <= 15 seconds - D`. If `15 seconds - D <= 0`, or the
   clocks cannot be aligned within ±1 second, report the denial attempt
   `NOT-RUN`. Do not proceed with the second tap if returning to Home leaves
   insufficient time in the derived window.
5. If the derived window remains after step 3, tap the visible label "Direct
   Message" once; this tap is `t2`. Do not press Back again. The verifier reads
   its native action timestamp from the same session and requires the exact denial log
   `show denied by Tinos rules (NOT_ALLOWED)` after that tap. Tie the log to
   the second tap by ordered timestamps. An exact denial is required; timing
   alone is not a denial. If the observed timestamps do not satisfy the bound
   in step 4, report the denial attempt `NOT-RUN`; do not count a late or absent
   denial as a pass.
6. Capture the final native Checker result and runner-bound final capture in
   the same Artemis session. Preserve the raw logcat, capture metadata,
   recording, action timestamps, Checker ledger, and final-capture identity.

If the first click logs `not loaded; skipping` or `show failed (<code>)`, it is
neither an allow nor a denial; report that case `NOT-RUN`. If the second click
logs either fill failure, it is neither a denial nor a pass; report the denial
attempt `NOT-RUN`. If both clicks complete inside the verified window but the
second click is allowed instead of denied, report `FAIL` with reason
`gate_allowed_inside_interval` when two allow lines and no denial are present.
If that timing window is missed, report `NOT-RUN` before classifying event
counts.
If the required revenue line is absent at final capture, report the distinct
result `FAIL: revenue_event_missing`; the revenue outcome is not a pass even
when the allow and denial logs exist. This case does not cover the 3-shows-per-hour cap.

## Negative control: false first-click denial

Case ID: `tinos-demo-first-click-negative-control`.

Use a separate cold start and a separate Artemis session. At the ready Home
screen, confirm visible label "WhatsApp" and visible label "Direct Message" in
the UI tree, then tap the visible label "Direct Message" exactly once. The
verifier requires exactly one matching action `tap` with `target_text` exactly
`Direct Message` in this session's `steps` data and reads its timestamp there;
do not enter a tap timestamp manually. Close the displayed
ad with its close control within 15 seconds of its first visible frame. Record
show and close timestamps from the recording; if the ad is not closed within
15 seconds, report `NOT-RUN`. After dismissal, require both
`show allowed: shown and dismissed` and a revenue line matching
`revenue <valueMicros> micros <currency> unit=ca-app-pub-3940256099942544/1033173712 network=<network> precision=<precision>`. The revenue assertion must pass.

The exact deliberately false final Checker assertion is:

> The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED).

The negative control is valid only when the same-session final Checker report
fails specifically because that denial assertion is false, while the required
allow and revenue observations pass. Record evidence schema verdict
`fail_assertion` only when the runner-bound final capture links to that Checker
trace. Preserve the false assertion, Checker trace identity, linked final
capture identity, click trace, recording, and raw `TinosDemo` logcat.

If the control log contains the denial line, reject the control as
`invalid_control`: the one-tap control must not attempt a second show. If
revenue is missing, record `invalid_control` with reason
`revenue_event_missing`; do not treat it as the intended failure. If the first
click logs `not loaded; skipping`, `show failed (<code>)`, or no exact allow,
record `invalid_control`; the control did not fail for the intended reason.
A pass, generic Checker failure, stale or mismatched capture, missing logcat,
an event outside the same-session step window, an event before the first tap,
or missing same-session linkage is also `invalid_control`. An orchestration
status such as `completed` is not sufficient. Do not apply the Pocket Actual
checker adapter unchanged to Tinos.

## Execution and reporting

Use the Tinos-specific manifest at
`qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json`.
It pins the exact runner argv, the log-capture wrapper argv, environment,
profile, dependency lock, full case-goal strings, case-specific verifier argv,
and revision policy. Submit each complete `case_goals` value verbatim as the
single `artemis run` goal argument. Do not pass a journey filename or invent a
prompt-loading step. Substitute only the manifest's listed runtime values.
Treat every template as argv elements, not a shell string. Run each case once
in the foreground with a fresh UUID per attempt; use that UUID for both
`--session-id` and `--run-id`. Keep each case's Checker result and final capture
in that same session. Do not substitute an MCP route, fill in omitted flags,
or run the negative control as a positive. Set
`ARTEMIS_TRACES_DIR=<attempt-root>/ledger` and
`DATA_ENGINE_DB_PATH=<attempt-root>/ledger/data_engine.db`; `--traces-path`
is only for Artemis trace/video recording.

Invoke the manifest's `log_capture_argv_template` to start capture before
Artemis. The helper captures a cursor just before its exact-serial logcat
process starts and before Artemis can begin the cold-start steps. It saves raw
output as `<attempt-root>/tinos-demo-logcat.txt`, saves cursor, serial, PID,
runner status and stopped status as `<attempt-root>/tinos-demo-logcat.meta`,
and stops the exact logger PID on every exit path. Its device command includes
`-s <Sol-granted-exact-serial>` and `-T <start-cursor>`; it does not clear or
replay the shared log buffer. Do not launch Artemis if capture startup fails.
SIGKILL can bypass the wrapper trap. After every outcome, confirm the logger
PID is gone before device release; if it remains, follow Sol's granted
cleanup route and do not release the device yet.

After the helper returns, run exactly the verifier argv template for the case,
replacing only listed runtime values. Use the separate `positive` and
`control` modes of `qualification/tools/tinos_demo_verdict_exit.py`. This is a
testcase-side tool, not a runner change. The verifier maps each device log
timestamp to the host epoch using the recorded device clock and bracketing host
samples. It rejects a relevant Tinos log event unless its full clock-uncertainty
interval is inside the exact session's `steps` window and strictly after the
first tap. It also rejects any event before the device-epoch cursor or a cursor
that may start after the first tap. The verifier checks the serial, cursor, PID,
clock samples and stop record against the helper metadata.
The control mode rejects any denial event. Its exit `1` with JSON
`fail_assertion` is valid only when Artemis exited `0`, the allow and revenue
observations pass, and the sole failed final Checker assertion is the exact
false assertion linked to the same-session native Checker trace and
runner-bound final capture. Control exit `2` with `NOT-RUN` means the allow is
missing after the timeout-race case; other control exit `2` results are
`invalid_control`. Control mode requires exactly one matching feature-tap step
in this same session. Never count an arbitrary Artemis process failure or a
completed orchestration status as control success.

Positive mode derives exactly two feature-tap timestamps from this same
session's `steps` rows matching action `tap` and `target_text` exactly
`Direct Message`. The executor reads `first_visible_at`
and `closed_at` from the recording and supplies only those two host-epoch-second
values to the verifier. The verifier maps device log
epochs to that host epoch using the capture clock samples. It requires every
recognized event's full clock-uncertainty interval to fall inside the same
session step window and strictly after the first tap. The verifier requires
the allow line after the close time, revenue after the first tap, and the exact
denial after the second tap. It calculates `D` from the two recording
times and checks both `(t2 - t1) <= 15 - D` and
`(t2 - t1) + D + 5 <= 20`. Positive exit `0` means `PASS`; exit `10` means
`FAIL` for a missing revenue or denial observation or for
`gate_allowed_inside_interval` when two allow lines and no denial occur inside
the verified timing window. The timing window is checked before allow-event
counts; a missed window returns `NOT-RUN`. Exit `11` means `NOT-RUN`
for fill failure, missing allow, a missed close or timing window; exit `12`
means `INVALID-EVIDENCE`. These positive-mode exit codes do not overlap the
negative-control result codes. Do not apply the Pocket Actual checker adapter
unchanged to Tinos.

Record each attempt's runner SHA, candidate commit SHA, manifest version and
digest, journey digest, APK source commit/package/size/SHA-256, exact wrapper,
runner and verifier argv, target class and Sol-granted serial, session/run ID,
attempt kind, action and log timestamps, capture cursor and logger PID, logger
stop status and post-outcome PID-gone confirmation, observed event order, raw
logcat, recording, native traces,
Checker verdict and trace ID, linked final-capture ID, Artemis exit status,
verifier JSON and exit status, cleanup result, and verified device release.
Keep the two case results separate. The negative control's expected
`fail_assertion` is not a positive pass.

No device command, claim, installation, launch, or execution was performed to
author this candidate. Do not begin execution if it requires new spend.
Android CLI XML journeys remain separate experimental evidence and cannot
substitute for Artemis acceptance.
