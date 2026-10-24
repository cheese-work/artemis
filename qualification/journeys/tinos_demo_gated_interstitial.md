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
`revenue <micros> <currency> unit=<adUnitId> network=<network> precision=<precision>`.
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
6. Select the WhatsApp tab using `home-tab-whatsapp`. Confirm `home-screen-2`
   and the Direct Message feature selector
   `home-screen-2-feature-direct_message` are visible. The selector is the
   feature row's app-defined Compose test tag.
7. Capture the timestamped screen recording, native action trace, and raw
   `TinosDemo` logcat for the same attempt. Record the logcat cursor before the
   first click; do not clear shared ADB or log buffers.
8. For each shown interstitial, close it with its visible close control within
   15 seconds of the first visible frame. Record show and close timestamps from
   the recording. If the ad is not closed within 15 seconds, report that
   attempt `NOT-RUN`; the allow line is emitted only after dismissal.

After each attempt, stop the app, preserve all raw evidence, and return the
exact granted AVD to Sol for verified cleanup and release. Record cleanup on
success and failure. Never reset shared ADB or touch another owner's device.

## Positive case: allow, then deny

Case ID: `tinos-demo-allow-then-interval-denial`.

This case uses ordinary Home UI actions for the non-premium test state. It does
not call the ad API directly.

1. At Home with the WhatsApp tab selected, tap the Direct Message row using
   `home-screen-2-feature-direct_message`. Preserve the native action trace and
   timestamped recording.
2. Close the displayed ad with its close control within 15 seconds of its first
   visible frame. Record the show and close timestamps from the recording and
   calculate `D` as the visible duration in seconds. If the ad is not closed
   within 15 seconds, report this attempt `NOT-RUN`. After dismissal, require
   `show allowed: shown and dismissed` for this tap. The app then navigates to
   Direct Message. Require at least one revenue line for this first tap that
   matches the format above and includes the exact test-unit `unit=` value.
   Evaluate this revenue requirement from the continuous logcat capture; do
   not delay the repeat tap to wait for it.
3. Return to Home with Android system Back once, not the app toolbar/back
   control. `NavDisplay` handles system Back by popping the route; the screen's
   own back callback invokes the separate `inter_back` ad path. Confirm Home,
   the WhatsApp tab, and the same Direct Message selector before tapping again.
4. Measure `t1` at the first exact allow-log timestamp after dismissal, and
   `t2` at the second feature tap's native action timestamp after returning to
   Home. Normalize the log, action, and recording timestamps to one monotonic
   timeline and record their offsets. Let `D` be the first ad's visible
   duration in seconds, from its first visible frame to close-control
   activation in the recording. The SDK records show history when the ad is
   shown, not dismissal, and checks the second show only after loading, which
   can take up to 4 seconds. Include the 1-second timestamp uncertainty in
   the required bound: `(t2 - t1) + D + 5 seconds <= 20 seconds`. Derive the
   tap window as `t2 - t1 <= 15 seconds - D`. If `15 seconds - D <= 0`, or the
   clocks cannot be aligned within ±1 second, report the denial attempt
   `NOT-RUN`.
5. After the ad closes, return to Home with Android system Back once, not the
   app toolbar/back control. Confirm Home, the WhatsApp tab, and the same
   Direct Message selector before tapping again. If dismissal, Back, or return
   to Home leaves insufficient time in the derived `15 seconds - D` window,
   do not make a late tap; report the denial attempt `NOT-RUN`. Otherwise, tap
   the same Direct Message selector once and require the exact denial log
   `show denied by Tinos rules (NOT_ALLOWED)` after that tap. Tie it to the
   second tap by ordered timestamps. An exact denial is required; timing alone
   is not a denial.
6. Capture the final native Checker result and runner-bound final capture in
   the same Artemis session. Preserve the raw logcat, recording, action
   timestamps, Checker ledger, and final-capture identity.

If the first click logs `not loaded; skipping` or `show failed (<code>)`, it is
neither an allow nor a denial; report that case `NOT-RUN`. If the second click
logs either fill failure, it is neither a denial nor a pass; report the denial
attempt `NOT-RUN`. If both clicks complete inside the verified window but the
second click is allowed instead of denied, report `FAIL` for the gate case.
If the required revenue line is absent at final capture, report the distinct
result `FAIL: revenue_event_missing`; the revenue outcome is not a pass even
when the allow and denial logs exist. This case does not cover the 3-shows-per-hour cap.

## Negative control: false first-click denial

Case ID: `tinos-demo-first-click-negative-control`.

Use a separate cold start and a separate Artemis session. At the ready Home
screen, tap the same Direct Message selector exactly once. Close the displayed
ad with its close control within 15 seconds of its first visible frame. Record
show and close timestamps from the recording; if the ad is not closed within
15 seconds, report `NOT-RUN`. After dismissal, require both
`show allowed: shown and dismissed` and a revenue line that includes
`unit=ca-app-pub-3940256099942544/1033173712`. The revenue assertion must pass.

The exact deliberately false final Checker assertion is:

> The first Direct Message feature click must be denied by Tinos rules (NOT_ALLOWED).

The negative control is valid only when the same-session final Checker report
fails specifically because that denial assertion is false, while the required
allow and revenue observations pass. Record evidence schema verdict
`fail_assertion` only when the runner-bound final capture links to that Checker
trace. Preserve the false assertion, Checker trace identity, linked final
capture identity, click trace, recording, and raw `TinosDemo` logcat.

If revenue is missing, record `invalid_control` with reason
`revenue_event_missing`; do not treat it as the intended failure. If the first
click logs `not loaded; skipping`, `show failed (<code>)`, or no exact allow,
record `invalid_control`; the control did not fail for the intended reason.
A pass, generic Checker failure, stale or mismatched capture, missing logcat,
or missing same-session linkage is also `invalid_control`. An orchestration
status such as `completed` is not sufficient. Do not apply the Pocket Actual
checker adapter unchanged to Tinos.

## Execution and reporting

Use the Tinos-specific manifest at
`qualification/manifests/tinos_demo_gated_interstitial.che2016.v1.json`.
It pins the exact CLI flags, profile, dependency lock, case goals, and revision
policy. Run each case once in the foreground with a fresh UUID per attempt;
use that UUID for both `--session-id` and `--run-id`. Keep each case's Checker
result and final capture in that same session. Do not substitute an MCP route,
fill in omitted flags, or run the negative control as a positive. The command
sets `ARTEMIS_TRACES_DIR` to `<attempt-root>/ledger` and
`DATA_ENGINE_DB_PATH` to `<attempt-root>/ledger/data_engine.db`; its
`--traces-path` is only for Artemis trace/video recording.

Record each attempt's runner SHA, candidate commit SHA, manifest version and
digest, journey digest, APK source commit/package/size/SHA-256, exact command,
target class and Sol-granted serial, session/run ID, attempt kind, action and
log timestamps, observed event order, raw logcat, recording, native traces,
Checker verdict and trace ID, linked final-capture ID, exit status, cleanup
result, and verified device release. Keep the two case results separate. The
negative control's expected `fail_assertion` is not a positive pass.

No device command, claim, installation, launch, or execution was performed to
author this candidate. Do not begin execution if it requires new spend.
Android CLI XML journeys remain separate experimental evidence and cannot
substitute for Artemis acceptance.
