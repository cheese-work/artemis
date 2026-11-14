# AP006 CHE-5938 exact-head device proof

This is a provisional five-case Artemis journey for AP006 candidate
`d406c3c17a38d11658724bac760113a0495d7ae1`. It is an authoring artifact, not
execution evidence or admission. Bind the journey digest in
`qualification/candidates/ap006_che5938.v1.json` before review. The candidate
APK must be built from that exact app SHA and its SHA-256 recorded before
installation.

## Runner and references

- Artemis runner: `cheese-work/artemis` at
  `581b7589e0e9d50ca7790e9053336ced7d2e0cfe`.
- Artemis task/assertion contract: `README.md` (Python SDK example and
  `TaskResult`) and `artemis/interfaces/cli/assertion_exit.py`.
- Test authoring and evidence references: `mcp_server/rules.md`,
  `CONTRIBUTING.md` (test layers and fake ADB), and
  `qualification/journeys/pocket_actual_save_relaunch.md` (fixture, positive
  and negative-control format).
- App source locator references are at the candidate SHA. A device UI dump
  must confirm runtime selectors before review; source resource IDs alone do
  not prove an element is visible or accessible.

## Shared execution contract

Use Artemis Pro because the cases require checkpoints, branching, diagnostics,
and a report. Use a dedicated, admitted Android target with API 27 or newer and
ABI `arm64-v8a` or `armeabi-v7a`; record exact model/image, API, ABI, serial,
host, Artemis run ID, task/session ID, app SHA, APK SHA-256, and ICT start/end
times. Sol supplies the target and grant after testcase reviews and admission.
Do not auto-select a device. Do not begin before Sol records the exact grant.

Before each case, install the pinned APK only after admission, preserve existing
apps and personal data, and use only the case's stated app configuration. Reset
only state created by that case. Record initial app state, Remote Config and
consent/premium values where observable, placement alias, lifecycle state, and
the test-ad fixture identity. Stop on an uncontrolled prerequisite and record
that assertion NOT-RUN; do not infer or simulate a pass.

Use resource IDs from the pinned app hierarchy when present; prefer IDs over
text and coordinates. Current source candidates include `ad_call_to_action`,
`nativeAdLayout`, `viewPager`, `tabLayout`, and `btnWatchFull`. Confirm each
candidate against the live hierarchy and bind a selector only after that
observation. Use text/OCR only for ad content without a resource ID, and record
the observed value. Never use guessed coordinates as a fallback.

Every positive case requires Artemis Pro plan checkpoints plus raw native
evidence listed below. A completed run with any failed assertion is FAIL. A
missing observation, inaccessible configuration, unsupported capture path,
or missing raw artifact is NOT-RUN. Infrastructure failure is reported
separately from an application assertion failure. Preserve the raw Artemis
trace/report, screenshots or recordings, relevant logcat interval, and a
manifest that maps each assertion to its artifact and ICT timestamp.

## Case 1: native test-ad click, external return, and one auto-next

**Setup:** Start from the pinned app's observed language/onboarding placement
that exposes the actual displayed native test ad. Record its exact alias and
the hierarchy selector for its visible CTA. Reset only app state created for
this attempt. Start one continuous, serial-scoped logcat capture before the ad
is displayed and stop it only after return and the next screen settles.

**Steps:** Wait for the real ad creative and CTA to be visible; record the
first displayed frame. Tap the visible CTA once. Observe the external app,
return to AP006, and wait for the app's normal auto-next behavior. Do not tap
any continuation control after returning. In a separate negative control,
repeat without a native-ad click and with an unrelated alias click if that
alias can be made visible through the approved configuration.

**Assertions:** The continuous interval contains the displayed ad, click,
external activity transition, AP006 return, and next activity transition in
order. The expected alias advances exactly once. No synthetic CTA forwarding
is observed. No advance occurs after plain pause/resume or for the unrelated
alias. If the unrelated alias cannot be selected using real app configuration,
mark only that assertion NOT-RUN.

**Evidence:** Preserve the full matching logcat interval, activity/alias
events, screenshots of the displayed ad and post-return screen, native Artemis
timeline, and ICT timestamps. Earlier logcat that starts after the click does
not satisfy this oracle.

## Case 2: displayed-ad three-second close gate

**Setup:** Use the actual native-full test placement, isolated to one attempt.
Record fetch completion, view binding, and the first frame that visibly
contains the real ad. The timer oracle starts at that first displayed frame,
not at the loading skeleton or fetch success.

**Steps:** Make one tap before 3.000 seconds from the displayed frame, then
observe the ad and underlying screen. Reset the case. Wait until at least
3.000 seconds from the displayed frame and tap the visible close control once.

**Assertions:** The early tap neither closes the ad nor opens the underlying
screen. The late tap closes the ad. Record measured display-to-close timing and
compare it with fetch and binding events; report any timer/display mismatch.
Do not change or assert native-full layout, UI, or UX beyond this close gate.

**Evidence:** Timestamped video or frame sequence showing first displayed
frame and both taps, hierarchy snapshots for the close control, and native
timeline. If a genuine ad or a stable close selector is unavailable, mark the
affected assertion NOT-RUN.

## Case 3: Explore video screenshot and recording protection

**Setup:** Navigate using observed selectors to Explore and start a real
video. Record the Explore/player activity identity and the current
`FLAG_SECURE` state. Use only the admitted target's privacy-safe screenshot and
recording mechanisms.

**Steps:** Attempt one screenshot and one short recording while playing,
repeat while paused, then repeat during the transition from Explore to Home.
After Home is stable and secure ownership has released, capture Home. If a
dedicated player is reachable through an observed control, perform the same
capture attempts there as a separately identified subcase.

**Assertions:** Captures taken while playing, paused, or during the protected
transition contain no protected video pixels. Home is capturable after the
secure owner releases. Window flags are recorded but are not sufficient alone:
compare the captured frames with the visible source frames. Mark the dedicated
player subcase NOT-RUN if it is not reachable through the pinned app workflow.

**Evidence:** Before/after screenshots, recording and frame samples, activity
transitions, window-flag observations, Artemis timeline, and ICT timestamps.
Do not mark a capture assertion PASS when the capture API itself was
unavailable.

## Case 4: Home native eligibility, lifecycle, and Welcome-back suppression

**Setup:** Use only a supported test configuration that controls consent,
premium status, placement enabled state, ad ID, and lifecycle/fullscreen
conditions. Record each actual value and how it was set. Identify the Home
native row using its confirmed hierarchy resource ID and placement alias.

**Steps:** With the row loaded, revoke eligibility and verify removal. Restore
eligibility and observe whether loading retries. Repeat with Home offscreen and
through pause/resume or view recreation. For Welcome-back, attempt the actual
placement once with ineligible state, then with invalid lifecycle/fullscreen
state, each from a clean app-created state.

**Assertions:** A stale or pending Home row disappears after eligibility loss;
restoration can retry; an offscreen Home does not load. Welcome-back is
suppressed for an ineligible actual placement and for invalid lifecycle or
fullscreen state. Do not claim a branch was covered unless the configuration
was observably applied. Mark each unexposed configuration NOT-RUN.

**Evidence:** Configuration snapshot, alias, row presence/absence screenshots,
load/request events when exposed, lifecycle/activity timeline, and Artemis
assertion ledger. If the test build exposes no control for a required branch,
record that branch as NOT-RUN with the missing control named.

## Case 5: cold-start Remote Config success and fallback

**Setup:** Confirm the pinned APK and current app data state. Record whether
Firebase DebugView access is available without changing production settings.
Use a bounded network profile supplied by the admitted test environment; do
not alter shared network state. Record the profile and actual connectivity.

**Steps:** Cold start once with successful config access. Capture launch through
the first usable screen. Reset only run-created app state. Repeat offline, then
repeat under a recorded slow-network profile if that profile is available.

**Assertions:** Successful config startup reaches a usable screen. Offline and
slow fallback do not crash the launcher and do not leave the splash unbounded.
Set a 30-second observation deadline from the launch intent; failure to reach a
usable screen by that deadline is FAIL, unless the run itself is interrupted,
in which case it is infrastructure NOT-RUN. Record DebugView access as
available or unavailable. DebugView event assertions and AdMob revenue/show
rate remain NOT-RUN unless the relevant access and data are actually observed;
do not claim uplift.

**Evidence:** Launch and first-usable-screen timestamps, screenshots, app
crash/log evidence, connectivity profile, config result when exposed, Artemis
trace/report, and DebugView access result. Never interpret cached config as a
successful network fetch.

## Qualification and cleanup

After both independent non-author testcase reviews accept this exact digest
and Sol grants admission, qualification for each device class requires one
clean positive pass, a clean rerun, and one intended assertion failure that
produces a failing exit with preserved artifacts. Keep those three results
separate. A class not used here does not gate this class. CHE-387 AC3 flake
baseline remains a separate required-acceptance prerequisite.

After every attempt, stop only processes and tasks owned by that run, restore
only run-created app state and settings, verify the target is back to its
recorded pre-attempt state, release the run-bound lease, and return cleanup
evidence to Sol. Sol independently verifies cleanup and release. If cleanup is
uncertain, the target remains unavailable with a named recovery owner.

This authored revision has no device, ADB, emulator, provider, app-install,
fixture-reset, or Artemis execution evidence. All five behavior checks are
NOT-RUN until an independently reviewed revision is admitted and executed.
