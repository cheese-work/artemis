# CHE-6852 ST008 Event Map Device Proof

This candidate is a static Artemis testcase. Its receipt is `qualification/candidates/che6852_st008_event_map.v1.json`. It contains no execution evidence. The runner and every goal are pinned by the receipt.

## Immutable inputs

- Runner: `https://github.com/cheese-work/artemis` at `676320699514778d7b589783cb5590ada8d4048f`.
- App source: `https://github.com/tevo-studio/st008-palios/pull/36`, head `aeb9879c06ece7998aa5c85012e6fe750b15bbb9`, base branch `release/1.0.8`, base SHA `e6e62764484ae593204d61359416c957f92477b2`.
- Frozen event map SHA-256: `e362eaf1564501a1986934508bce53d5fcd6e68300d464cbca5cef2e151d99c8`.
- Effective lock SHA-256: `14f129e59028406dc1f77687c6c01ac83360926c91ea53c61a8f3a5b73dac080`.
- Local `tevo_ads` 0.3.2 override commit: `65e2050d61683151559580802de9697171788aa3`.
- Input packet SHA-256 values are recorded in the receipt. The downloaded copies are not runtime evidence.

## Admission and setup

Do not start this testcase until two independent non-author reviewers accept the same journey and receipt digests. The Android device squad must then issue the exclusive claim and active lease for the exact target serial. The leader owns admission; the executor uses only the granted serial and actions. The target class is `x99_api35_qual` (API 35, x86_64); its serial is `assigned_at_execution`. Class creation, admission and qualification belong to CHE-6297 / CHE-6843, not this authoring task.

Before each positive or qualification attempt, record the device class, OS/API/ABI, serial, testcase revision, runner SHA, app source SHA, build inputs and fresh attempt ID. Reset through the approved clean-install/first-run fixture for onboarding. Before each focused re-entry scenario, return to the named route through the UI and record its visible state. Preserve pre-existing device data; do not use broad device reset. Capture privacy-safe screenshots/hierarchy and `FA` / `FA-SVC` logs with timestamps. Redact names, account identifiers, tokens, transcripts, audio, lesson/session IDs, URLs, purchase identifiers and other personal values before retaining or attaching evidence.

Build the debug x86_64 APK on X99 from the exact app head, using the attached effective lock and recorded local `tevo_ads` override. Record the full build command, toolchain, source/lock/override identities and SHA-256 of the resulting APK before install. Verify package `com.palio.ai`, debug variant, version, and x86_64 ABI from that artifact. Install only that rebuilt artifact. Record its runtime APK digest in the execution evidence; do not add the digest to this static testcase. The testcase remains valid when that run-time digest is added because its identity pins the source, lock, override, runner, fixture contract and journey digest, while the APK digest identifies the artifact built from those inputs for that individual run. A source, lock, override, runner or journey change requires a new candidate revision and fresh reviews.

For every non-permission goal, snapshot `POST_NOTIFICATIONS` grant state and flags on the leased serial, run `adb -s <leased serial> shell pm clear com.palio.ai`, then run `adb -s <leased serial> shell pm grant com.palio.ai android.permission.POST_NOTIFICATIONS` before launch. Restore the exact snapshotted grant state and flags after each attempt. The runnable clean first-run fixture uses locale `en` and synthetic name Artemis QA; the separate negative control uses the same reset and locale on the Welcome onboarding screen. Do not use these commands until admission, both reviews and the active lease exist. Never reset shared device state.

The guide's four FA debug commands are pinned with the leased serial: set `debug.firebase.analytics.app` to `com.palio.ai`, set `log.tag.FA` and `log.tag.FA-SVC` to `VERBOSE`, then capture `logcat -v threadtime -s FA FA-SVC` to `<run evidence dir>/fa-fa-svc-threadtime.log`. Before changing properties, capture each current value with `getprop`. Restore each exact captured value after capture; do not assume `.none.`. Stop only the run-owned logcat capture after the observation window. Run the pinned host-side `qualification/fa_log_counter.py` on this saved file; retain the counter ledger and its input SHA-256. These are executor instructions, not commands run during authoring.

The pinned clean first-run fixture is the app-data reset and notification grant above, with locale `en`. Under this fixture, `event_01` through `event_09`, `removal_02`, `removal_03` and `removal_04` are runnable. `removal_03` is counted by goal_05 from the exact goal_01 log window. `removal_01` stays NOT-RUN because the fixture does not exercise app backgrounding. `removal_07`, `removal_08` and `removal_12` stay NOT-RUN because the fixture does not reach Home/Profile or induce an API failure. Stop at the login redirect; `event_11` and `removal_06` remain NOT-RUN because no authorized nonpersonal Google test account is available. Permission rows, speaking, flash-card, ad and paywall rows remain NOT-RUN as recorded in the receipt. Do not invent backend sessions, decks, callbacks, offers or purchases.

Do not call a model provider during authoring. The pinned config routes Artemis to OpenAI `gpt-6-sol` with `OPENAI_API_KEY`; planner validation is disabled. The recorded provider decision authorizes only this provider/model, up to USD 5 per run and USD 15 total. No provider call may occur until both independent reviewers approve this exact candidate and the Android device squad records AVD admission. Never record credential values or add Google as a provider.

## Synchronization and log oracle

Use visible UI state and timestamps to synchronize; do not infer a displayed screen, OS dialog, ad impression, store purchase, or successful feature result from a tap, route, callback request or empty shell. Keep the UI foreground unless a scenario explicitly exercises focus loss. Start the privacy-safe log capture before the action and stop it only after the expected event window closes. Correlate events by sequence and timestamp, not by personal identifiers. Validate exact event names, allowed parameter keys/values, counts and ordering against the frozen matrix. For removal families, count only custom events with origin `app` in the FA-SVC log; automatic Firebase events do not count. Do not disable automatic lifecycle, screen, ad or purchase collection.

The pinned host-side counter reads `<run evidence dir>/fa-fa-svc-threadtime.log` and emits one row per receipt `assertion_id`, with count, line order, origin, event name, raw parameters and input SHA-256. A malformed FA-SVC event line stops the counter and leaves affected rows NOT-RUN. Source inspection or the app adapter's log call alone cannot prove SDK output or the absence of extra events. Keep raw logs private during collection; retain only the minimum redacted FA/FA-SVC evidence needed to support each assertion. If the SDK emission surface cannot be observed, mark affected assertions NOT-RUN, not PASS. A row is OBSERVED only when the native ledger has a passed item naming its `assertion_id`, checked count and parameters, and the counter ledger for the same capture has the matching assertion id, count, raw parameters and order. Exit 0 alone sets no row status; absent or unchecked items remain NOT-RUN with unknown count.

## Pinned Artemis goals

Use the cheese-work/artemis PR #311 checkout. Record its exact published testcase commit SHA and verify git rev-parse HEAD matches before execution. Run only goals with runnable assertion IDs in the receipt, using its exact command template and `<candidate head SHA from CHE-6843 handoff>`. Resolve the config path with `realpath` from the PR #311 checkout root and stop unless the absolute path and SHA-256 match the receipt. Run with `env -u GOOGLE_API_KEY -u GEMINI_API_KEY`; before startup, inspect variable names only in the workspace `.env`, `get_env_file()` result and `GLOBAL_APP_DIR/.env`, and stop if any sets a Google key. The pinned config sends the pixel safety net and planner validation to OpenAI `gpt-6-sol`; the command disables planner validation. Set fresh, distinct UUIDv4 session and run IDs for every invocation. Keep each run's trace directory separate. Record metered spend per run and cumulative spend; stop before exceeding USD 5 per run or USD 15 total. Do not add Google as a provider.

When the device policy is enabled for the leased serial, `--locked-app com.palio.ai` requires the resumed package to equal the locked package and latches `LeftTestApp` otherwise; the permission-controller dialog has a different resumed package. On an unrestricted serial this lock is not enforced as a foreground lock. Runtime behavior is NOT-RUN. Goal 2 remains DO NOT INVOKE; no device check was made for this conclusion.

The overlapping notification/microphone requests and bridge-error recovery cases are NOT-RUN with the current runner lock. Do not claim safe speaking recovery from an interaction the locked run cannot complete.

Runnable goal text binds each assertion row to a named native assertion item. For each applicable positive trigger, compare exact event name, allowed parameter keys and values, trigger count, and order in the complete timestamped SDK window. Each removal item requires count 0 only when its relevant flow ran and the full SDK window is captured. Otherwise mark it NOT-RUN. A planner-generated partial assertion list is not sufficient; preserve the exact goal text, native summary, assertion ledger, trace, screenshots/hierarchy and sanitized logs. Rows marked DEFERRED or NOT-RUN appear only in the receipt, never as runnable goal assertions.

### Goal 1: Fresh onboarding and permissions

Use the pinned clean first-run fixture: reset com.palio.ai, grant POST_NOTIFICATIONS before launch, set locale en, and enter only synthetic name Artemis QA. Stop at the login redirect without signing in. Capture the complete timestamped FA/FA-SVC window and visible states. Assert event_01=splash_scr_view, event_02=obd_1_scr_view, event_03=obd_2_scr_view, event_04=obd_3_scr_view, event_05=lfo_1_scr_view, event_06=lfo_2_scr_view with language=en, event_07=obd_6_scr_view, event_08=obd_7_scr_view, event_09=obd_8_scr_view, exactly once per visible trigger and in observed step order with each row's expected parameters. Assert removal_02=splash_bootstrap_started|splash_bootstrap_completed|splash_bootstrap_failed and removal_04=language_selected|onboarding_started|onboarding_next_tapped|onboarding_completed each have count 0 in the complete captured window. Missing UI or incomplete capture is not PASS.

### Goal 2: Permission prompt visibility and overlap

DO NOT INVOKE. event_12 through event_16 and removal_05 are NOT-RUN because permission-controller dialog behavior is not verified under the pinned locked-app policy.

### Goal 3: Speaking and flash-card attempt counts

DO NOT INVOKE. event_20 through event_27 and removal_09 are NOT-RUN because no deterministic speaking-session or flash-card deck fixture is pinned.

Compare speaking/flash-card real-return counts to the contract for each accepted attempt; any mismatch fails.

### Goal 4: Ads, paywall and SDK emissions

DO NOT INVOKE. event_17 through event_19, event_28 through event_32, removal_10 and removal_11 are NOT-RUN because verified ad callbacks, real store offers, and authorized sandbox fixtures are not pinned. Never fabricate impressions or purchases or disable automatic collection.

### Goal 5: Removal inventory and qualification controls

Bind goal_05 to the complete FA/FA-SVC log captured during goal_01; do not launch a second fixture. Run the pinned `qualification/fa_log_counter.py` command from the receipt against `<run evidence dir>/fa-fa-svc-threadtime.log`. Assert removal_03=navigation_route_changed|screen_view has count 0 in that capture. Count only custom events with origin=app in FA-SVC; automatic Firebase events do not count for removal families. Preserve automatic Firebase collection. The counter output must include the receipt assertion_id, count, source line order, event origin, event name, raw parameters and source-log SHA-256.

### Deliberate negative control

Run the pinned clean onboarding fixture as a separate qualification attempt with locale en and `--verification-level final`. On the Welcome onboarding screen, assert assertion_id=negative_control_title: visible text from `lib/presentation/l10n/app_en.arb` key `onboardingWelcomeMessage` is exactly `Palios — WRONG TITLE`; the real value is `Hello! I'm Palios, your personal AI tutor. I will help you learn a new language and communicate fluently.` The native summary must contain exactly one failed item for this assertion, stdout must contain ARTEMIS_ASSERTION_EXIT=1, and process exit must equal 1. Keep this expected failure separate from positive runs; exit 0, exit 2, a missing marker, incomplete evidence or another failed item invalidates the control.

## Event assertions

Every assertion below is `NOT-RUN` until the reviewed testcase executes. The event label binds to the exact event name. The row-specific expected trigger and parameters are in the receipt and frozen event matrix. `observed_count: unknown` is intentional and never means zero.

### event_01: `splash_scr_view`
Assert the splash first becomes visible before Get Started/bootstrap work. **Fail closed:** no visible splash evidence or wrong count fails.

### event_02: `obd_1_scr_view`
Assert the welcome screen is visible. **Fail closed:** route/state without visible screen evidence fails.

### event_03: `obd_2_scr_view`
Assert the questions introduction is visible. **Fail closed:** a skipped or hidden step cannot pass.

### event_04: `obd_3_scr_view`
Assert the name form is visible without retaining the entered name. **Fail closed:** personal name data in retained evidence invalidates the assertion.

### event_05: `lfo_1_scr_view`
Assert the first learning-language screen is visible. **Fail closed:** selection or rebuild alone is not a screen view.

### event_06: `lfo_2_scr_view`
Assert the distinct native-language screen is visible and `language` equals the selected locale tag on entry, including preselection. **Fail closed:** wrong/missing locale or an event caused only by a row selection fails.

### event_07: `obd_6_scr_view`
Assert the goal step is visible with the original onboarding ordinal preserved. **Fail closed:** invented/renumbered steps fail.

### event_08: `obd_7_scr_view`
Assert the level step is visible. **Fail closed:** route transition without visible state fails.

### event_09: `obd_8_scr_view`
Assert the talk-style step is visible. **Fail closed:** route transition without visible state fails.

### event_10: `obd_full_8_scr_view`
This event stays deferred. **Fail closed:** never claim an observation; the empty shell, skipped route or unverified SDK cannot pass.

### event_11: `home_scr_view`
Assert Home is actually visible after redirects and any covering Welcome Back screen, and on real return. **Fail closed:** covered Home or route-only state fails.

### event_12: `noti_permission_view`
Assert a real OS notification request dialog is displayed with `source=app_entry` or `account`. **Fail closed:** focus loss, pending call, already-granted state, unsupported OS, suppressed repeat or bridge error is not a displayed prompt.

### event_13: `noti_permission_allow`
Assert the displayed notification request receives OS grant. **Fail closed:** no preceding displayed prompt or a non-grant fails.

### event_14: `microphone_permission_view`
Assert a real OS microphone dialog is displayed for speaking with `source=speaking`. **Fail closed:** permission precheck, focus loss or bridge invocation alone fails.

### event_15: `microphone_permission_allow`
Assert the displayed microphone request receives OS grant. **Fail closed:** no preceding displayed prompt or a non-grant fails.

### event_16: `microphone_permission_denied`
Assert the displayed microphone request receives OS denial. **Fail closed:** no preceding displayed prompt or a non-denial fails.

### event_17: `inter_splash_scr_view`
Assert the existing splash interstitial callback corresponds to a real displayed impression. **Fail closed:** preload, request, failure, suppression or callback without visible impression fails.

### event_18: `inter_home_scr_view`
Assert the shared Home/lesson/tab placement emits once for one actual impression. **Fail closed:** duplicate caller callbacks or non-impression paths fail.

### event_19: `app_open_scr_view`
Assert `placement=open_resume` follows a confirmed app-open impression after resumed Welcome Back Continue. **Fail closed:** cold-start Welcome Back or request without impression fails.

### event_20: `feature_speaking_scr_view`
Assert the Speaking entry is visible from Mini Test or Home. **Fail closed:** tap or route without visible screen fails.

### event_21: `feature_speaking_start`
Assert one event for each accepted transition to `starting`, before permission/network work. **Fail closed:** rejected, duplicate or stale start does not count as an accepted run.

### event_22: `feature_speaking_finish`
Assert one event when a normal result exists and `canShowResult` is ready for presentation. **Fail closed:** timer-only close, null result, error fallback, failed recovery or duplicate callback fails.

### event_23: `feature_speaking_fail`
Assert a terminal error uses only the bounded `error_code` enum. **Fail closed:** recoverable socket interruption that recovers, cancellation or unbounded/personal error text fails.

### event_24: `feature_flash_card_scr_view`
Assert one visible feature entry for direct deck or session-vocabulary intro followed by its same-session deck. **Fail closed:** intro-to-deck duplication or hidden route fails.

### event_25: `feature_flash_card_start`
Assert accepted generation/load once per attempt; direct load, explicit retry or restart starts a new attempt. **Fail closed:** provider replay or intro-to-deck double count fails.

### event_26: `feature_flash_card_success`
Assert success occurs once on classification of the last real word in a nonempty deck and first completion transition. **Fail closed:** empty deck, stale gesture or replay fails.

### event_27: `feature_flash_card_fail`
Assert terminal load/generation failure uses only `load_failed`, `empty_deck`, `timeout` or `unknown`. **Fail closed:** empty deck reported as success or recoverable pronunciation failure as whole-deck failure fails.

### event_28: `paywall_scr_view`
Assert the visible weekly trial paywall emits `paywall_scr_view` with the original `source` and `paywall_id=premium_weekly_trial`. **Fail closed:** the exit-discount sheet ID, a hidden route, a wrong source or weekly re-emission after sheet dismissal fails this row.

### event_29: `paywall_scr_view`
Assert the visible exit-discount sheet over the weekly paywall emits `paywall_scr_view` with the original `source` and `paywall_id=premium_yearly_exit`. **Fail closed:** the weekly paywall ID, route-only appearance, missing sheet emission or duplicate visibility event fails this row.

### event_30: `paywall_btn_click`
Assert accepted weekly purchase, restore, terms, privacy or close action records the allowed `button`, real store product ID or `none`, and original `source`. **Fail closed:** disabled/busy/duplicate action or a fabricated product ID fails.

### event_31: `paywall_btn_click`
Assert yearly purchase or exit-sheet dismissal records `purchase` or `close`, the real yearly store product ID for purchase or `none` otherwise, and original `source`. **Fail closed:** programmatic dismiss after purchase or duplicate close fails.

### event_32: `in_app_purchase`
Assert automatic Firebase/Google Play purchase collection only in the authorized store sandbox. **Fail closed:** no fixture means NOT-RUN; manual emission, fabricated purchase or spend outside authorization is prohibited.

## Removal assertions

Each assertion applies to the complete list of exact removed names in the receipt. Exercise its relevant flow and inspect actual SDK emissions for the full observation window. Preserve the user action and app behavior. **Fail closed:** any removed custom name observed fails; unavailable/incomplete capture is NOT-RUN, never an absence pass.

### removal_01: lifecycle custom events
Check `app_open`, `app_foreground`, `app_background`.
**Fail closed:** an exact custom emission fails; automatic Firebase lifecycle collection remains enabled.

### removal_02: splash bootstrap events
Check `splash_bootstrap_started`, `splash_bootstrap_completed`, `splash_bootstrap_failed`.
**Fail closed:** an exact custom emission fails.

### removal_03: generic route events
Check `navigation_route_changed`, `screen_view`.
**Fail closed:** manual generic emission fails; automatic screen collection remains enabled.

### removal_04: onboarding funnel events
Check `language_selected`, `onboarding_started`, `onboarding_next_tapped`, `onboarding_completed`.
**Fail closed:** any removed custom event fails; mapped visible-step events remain required.

### removal_05: permission tap/result events
Check `permission_request_tapped`, `permission_result`, `account_notifications_enable_tapped`, `account_notification_permission_result`.
**Fail closed:** any removed custom event fails; actual prompt visibility/result rules remain required.

### removal_06: authentication events
Check `login_google_start`, `login_google_success`, `login_google_cancelled`, `login_google_failed`, `login_backend_failed`, `login_google_error`, `logout_tapped`, `logout_success`.
**Fail closed:** any removed custom event fails; auth behavior and nonpersonal user properties remain intact.

### removal_07: tab-selection events
Check `home_tab_selected`, `practice_tab_selected`, `redeem_tab_selected`, `rank_tab_selected`, `account_tab_selected`.
**Fail closed:** any removed custom event fails; Home view still requires actual Home visibility.

### removal_08: account settings events
Check `account_language_opened`, `account_language_changed`, `account_rate_app_tapped`, `account_contact_us_tapped`, `account_share_app_tapped`.
**Fail closed:** any removed custom event fails; settings actions remain usable.

### removal_09: lesson lifecycle events
Check `lesson_selected`, `lesson_start`, `lesson_end`, `lesson_complete`.
**Fail closed:** any removed custom event fails; speaking attempt events retain the specified start/finish timing.

### removal_10: generic interstitial events
Check `interstitial_show`, `interstitial_failed`.
**Fail closed:** any removed custom event fails; exact placement impression events remain separately checked.

### removal_11: purchase outcome events
Check `premium_purchase_attempt`, `premium_purchase_success`, `premium_purchase_cancelled`, `premium_purchase_failed`, `premium_restore_attempt`, `premium_restore_success`, `premium_restore_failed`.
**Fail closed:** any removed custom event fails; accepted UI clicks and automatic purchase collection remain distinct.

### removal_12: generic API failure events
Check `api_call_failed`, `api_call_timeout`.
**Fail closed:** any removed custom event fails; API failures and UI handling still work.

## Class qualification and result handling

If `x99_api35_qual` is unqualified, the first admitted mutating run is its qualification under the same two-review gate. It includes a clean representative pass, a clean rerun, and a deliberate failing assertion with native completed assertion exit 1. Keep each run's fixture/reset evidence, raw native summary and failure ledger, timestamps, screenshots/hierarchy, sanitized FA/FA-SVC logs, exact command, target serial, runner and testcase digest, and app/APK identities. The negative control is expected to fail and is not an app regression. Exit 2, missing artifacts or incomplete cleanup is inconclusive, never a qualification pass.

At the end of every attempt, preserve evidence before stopping only run-owned processes. Restore only settings changed by the run. Verify process, lease and device cleanup/release with the Android leader; unresolved cleanup leaves the target unavailable with a named recovery owner. Return event/removal counts, exact parameters, observed/failed/NOT-RUN state, fixture prerequisites, failure category, cleanup evidence and actual Diff-Digest for the real app base/head. Never infer zero from missing logs. Do not change expectations during execution.

`obd_full_8_scr_view` stays deferred until its separately supported native impression exists. DebugView and store-sandbox purchase stay NOT-RUN until their respective fixture and access are recorded. No SDK publication/substitution, provider call or spend occurs as part of testcase authoring.
