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

Reset only the admitted package with `adb -s <leased serial> shell pm clear com.palio.ai`. Before permission scenarios, the executor must snapshot permission state, then revoke `android.permission.POST_NOTIFICATIONS` and `android.permission.RECORD_AUDIO` and clear only their `user-set` and `user-fixed` flags with `pm clear-permission-flags`. Do not use these commands until admission, both reviews and the active lease exist. Never reset shared device state.

The guide's four FA debug commands are pinned with the leased serial: set `debug.firebase.analytics.app` to `com.palio.ai`, set `log.tag.FA` and `log.tag.FA-SVC` to `VERBOSE`, then capture `logcat -v threadtime -s FA FA-SVC`. Before changing properties, capture each current value with `getprop`. Restore each exact captured value after capture; do not assume `.none.`. Stop only the run-owned logcat capture after the observation window. These are executor instructions, not commands run during authoring.

No approved clean first-run fixture or authorized nonpersonal Google login account is available. Until the Android leader records the clean first-run fixture, `event_01` through `event_09`, `removal_02`, and `removal_04` remain NOT-RUN with unknown counts. Stop at the login redirect; `event_11` and `removal_06` remain NOT-RUN. Speaking, flash-card, ad, and paywall fixtures are also not pinned, so their mapped rows remain NOT-RUN. Do not invent backend sessions, decks, callbacks, offers or purchases.

Do not call a model provider during authoring. The pinned config routes Artemis to OpenAI `gpt-6-sol` with `OPENAI_API_KEY`; planner validation is disabled. The recorded provider decision authorizes only this provider/model, up to USD 5 per run and USD 15 total. No provider call may occur until both independent reviewers approve this exact candidate and the Android device squad records AVD admission. Never record credential values or add Google as a provider.

## Synchronization and log oracle

Use visible UI state and timestamps to synchronize; do not infer a displayed screen, OS dialog, ad impression, store purchase, or successful feature result from a tap, route, callback request or empty shell. Keep the UI foreground unless a scenario explicitly exercises focus loss. Start the privacy-safe log capture before the action and stop it only after the expected event window closes. Correlate events by sequence and timestamp, not by personal identifiers. Validate exact event names, allowed parameter keys/values, counts and ordering against the frozen matrix. Do not count automatic Firebase events as manual custom events, and do not disable automatic lifecycle, screen, ad or purchase collection.

The log oracle must read actual Firebase Analytics emissions, including transitive SDK emissions. Source inspection or the app adapter's log call alone cannot prove SDK output or the absence of extra events. Keep raw logs private during collection; retain only the minimum redacted FA/FA-SVC evidence needed to support each assertion. If the SDK emission surface cannot be observed, mark affected assertions NOT-RUN, not PASS.

## Pinned Artemis goals

Run the exact goal text and order from the receipt with this command template: `ARTEMIS_ARTEMIS_JSONC=qualification/configs/che6852_st008_openai.artemis.jsonc artemis run '<exact goal text>' --standalone --profile pro --assertion-exit --device-serial <leased serial> --locked-app com.palio.ai --disable-planner-validation --verification-level strict --session-id <fresh UUIDv4> --run-id <fresh UUIDv4> --traces-path <run evidence dir>/traces`. Set fresh, distinct UUIDv4 session and run IDs for every invocation. Keep each run's trace directory separate. The config path and SHA-256 are in the receipt. It pins the pixel safety net and planner validation to OpenAI `gpt-6-sol`; planner validation is also explicitly disabled by the command. No Google provider is configured.

`--locked-app com.palio.ai` does **not** permit the permission-controller package under the pinned runner's static foreground policy. `artemis/runtime/device_policy.py` requires the resumed package to equal the locked package and latches `LeftTestApp` otherwise. Permission-dialog runtime behavior is NOT-RUN. Keep `event_12` through `event_16` and `removal_05` NOT-RUN until runner policy changes and both reviewers accept a new candidate. No device check was made for this conclusion.

The overlapping notification/microphone requests and bridge-error recovery cases are NOT-RUN with the current runner lock. Do not claim safe speaking recovery from an interaction the locked run cannot complete.

The goal text binds each event and removal family to a named assertion item. For each applicable positive trigger, compare exact event name, allowed parameter keys and values, trigger count, and order in the complete timestamped SDK window. Each removal item requires count 0 only when its relevant flow ran and the full SDK window is captured. Otherwise mark it NOT-RUN. A planner-generated partial assertion list is not sufficient; preserve the exact goal text, native summary, assertion ledger, trace, screenshots/hierarchy and sanitized logs.

### Goal 1: Fresh onboarding and permissions

NOT-RUN until the Android leader records an approved clean first-run fixture: event_01 through event_09, removal_02, and removal_04 have unknown counts. With that fixture, observe and assert every listed visible onboarding step in order; enter only the synthetic name Artemis QA; verify the native locale on entry; stop at the login redirect without signing in; capture timestamped FA/FA-SVC logs and visible-state evidence. Assert event_01=splash_scr_view, event_02=obd_1_scr_view, event_03=obd_2_scr_view, event_04=obd_3_scr_view, event_05=lfo_1_scr_view, event_06=lfo_2_scr_view with language=<selected locale tag on entry: en|hi|pt-pt|es-mx|es-es|fr|vi>, event_07=obd_6_scr_view, event_08=obd_7_scr_view, event_09=obd_8_scr_view, event_10=obd_full_8_scr_view as DEFERRED only, event_11=home_scr_view as NOT-RUN without an authorized test login. Assert exactly one event for each visible trigger from event_01 through event_09, with only its row parameters and the observed step order. Assert removal_02=splash_bootstrap_started|splash_bootstrap_completed|splash_bootstrap_failed and removal_04=language_selected|onboarding_started|onboarding_next_tapped|onboarding_completed absent with count 0 in the complete captured onboarding window; otherwise mark NOT-RUN. Missing UI or incomplete capture is not PASS.

### Goal 2: Permission prompt visibility and overlap

NOT-RUN: --locked-app com.palio.ai does not permit the Android permission-controller foreground package, so do not attempt notification or microphone OS dialogs. Keep event_12=noti_permission_view with source=app_entry|account, event_13=noti_permission_allow, event_14=microphone_permission_view with source=speaking, event_15=microphone_permission_allow, event_16=microphone_permission_denied and removal_05=permission_request_tapped|permission_result|account_notifications_enable_tapped|account_notification_permission_result explicitly NOT-RUN. Record no observed count for these rows. A future admitted run may assert these only after the runner lock policy changes and both reviewers approve that candidate. No focus loss or pending call is proof of a displayed dialog.

### Goal 3: Speaking and flash-card attempt counts

NOT-RUN until the speaking session and flash-card deck fixtures are pinned by the Android leader. Required assertion items are event_20=feature_speaking_scr_view, event_21=feature_speaking_start, event_22=feature_speaking_finish, event_23=feature_speaking_fail with error_code=microphone_denied|start_failed|connection_failed|invalid_token|timeout|unknown, event_24=feature_flash_card_scr_view, event_25=feature_flash_card_start, event_26=feature_flash_card_success, event_27=feature_flash_card_fail with error_code=load_failed|empty_deck|timeout|unknown, and removal_09=lesson_selected|lesson_start|lesson_end|lesson_complete. In a fixture-backed run, assert one start per accepted attempt, at most one real finish per ready result, one fail per terminal failed attempt, one feature view per session, one flash-card start per attempt, one success per nonempty deck completion, and one fail per terminal deck failure. Preserve native ledger and timestamped logs; no fixture means NOT-RUN, with count unknown and never zero.

Compare speaking/flash-card real-return counts to the contract for each accepted attempt; any mismatch fails.

### Goal 4: Ads, paywall and SDK emissions

NOT-RUN until verified ad callbacks, real store offers and authorized sandbox fixtures exist. Required assertion items are event_17=inter_splash_scr_view, event_18=inter_home_scr_view, event_19=app_open_scr_view with placement=open_resume, event_28=paywall_scr_view with source=home|speaking|direct and paywall_id=premium_weekly_trial, event_29=paywall_scr_view with source=home|speaking|direct and paywall_id=premium_yearly_exit, event_30=paywall_btn_click with button=purchase|restore|terms_of_use|privacy_policy|close, product_id=<real weekly product ID or none>, source=home|speaking|direct, event_31=paywall_btn_click with button=purchase|close, product_id=<real yearly product ID for purchase or none>, source=home|speaking|direct, event_32=in_app_purchase with automatic schema only, removal_10=interstitial_show|interstitial_failed, and removal_11=premium_purchase_attempt|premium_purchase_success|premium_purchase_cancelled|premium_purchase_failed|premium_restore_attempt|premium_restore_success|premium_restore_failed. If later fixture-backed, assert exactly one event per accepted impression or click trigger and inspect actual transitive SDK emissions. No fixture means NOT-RUN and count unknown. Never fabricate impressions or purchases, disable automatic collection, or spend money.

### Goal 5: Removal inventory and qualification controls

For each complete actual SDK emission window, assert zero occurrences for each exact removed family: removal_01=app_open|app_foreground|app_background; removal_02=splash_bootstrap_started|splash_bootstrap_completed|splash_bootstrap_failed; removal_03=navigation_route_changed|screen_view; removal_04=language_selected|onboarding_started|onboarding_next_tapped|onboarding_completed; removal_05=permission_request_tapped|permission_result|account_notifications_enable_tapped|account_notification_permission_result; removal_06=login_google_start|login_google_success|login_google_cancelled|login_google_failed|login_backend_failed|login_google_error|logout_tapped|logout_success; removal_07=home_tab_selected|practice_tab_selected|redeem_tab_selected|rank_tab_selected|account_tab_selected; removal_08=account_language_opened|account_language_changed|account_rate_app_tapped|account_contact_us_tapped|account_share_app_tapped; removal_09=lesson_selected|lesson_start|lesson_end|lesson_complete; removal_10=interstitial_show|interstitial_failed; removal_11=premium_purchase_attempt|premium_purchase_success|premium_purchase_cancelled|premium_purchase_failed|premium_restore_attempt|premium_restore_success|premium_restore_failed; removal_12=api_call_failed|api_call_timeout. Mark a family NOT-RUN when its flow fixture or complete SDK capture is unavailable; missing logs are not zero. Preserve automatic Firebase collection. If the device class is unqualified, use the qualification negative control assertion_id=negative_control_title with wrong value `Palios — WRONG TITLE`; require one failed native item, ARTEMIS_ASSERTION_EXIT=1 and process exit 1. Retain artifacts and obtain leader cleanup/release verification.

### Deliberate negative control

Run the approved clean onboarding fixture as a separate qualification attempt. Assert assertion_id=negative_control_title: the visible Palios app title is exactly `Palios — WRONG TITLE`. The native summary must contain exactly one failed item for this assertion, the observed title must differ from the wrong value, stdout must contain ARTEMIS_ASSERTION_EXIT=1, and the process exit must equal 1. Keep this expected failure separate from positive runs; exit 0, exit 2, a missing marker, incomplete evidence or another failed item invalidates the control.

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
