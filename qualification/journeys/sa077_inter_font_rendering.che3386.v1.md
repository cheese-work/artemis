# CHE-3386 SA077 Bundled Inter Rendering

This is an authoring candidate for CHE-3385. It is a testcase definition, not execution evidence.

## Pinned inputs

- App repository: `https://github.com/tevo-studio/sa077-astrology-3`.
- App commit: `05c98f602bc0a211381000db82487caef0d5cb8a`.
- Package: `com.horoscope.tarotreader.palmistry.facereader`.
- Expected APK SHA-256: `1d2315b9004695a6f0bab6c1e82e703f1e5a6b29d10887c8d6e2a564f58aee02`.
- Runner: ARTEMIS CLI from `https://github.com/cheese-work/artemis` at `a14514a4508c366344c367aa39f672454de9c38f`.
- Device requirement: admitted X99 AVD class `x99_api35_medium_phone`, API 35, x86_64. The exact serial is supplied by the Android device squad after an exclusive claim.

Do not substitute the APK, app commit, runner commit, or device class. Verify the APK digest before install. A missing or mismatched artifact is BLOCKED, not permission to rebuild or use another artifact.

## Repeatable fixture and cleanup

Run only after two independent non-author reviews accept this exact candidate revision and the Android device squad grants an exclusive device claim. This testcase does not authorize device access, install, launch, reset, ADB, or execution.

After admission, start from a clean install or clear only this package's app data on the claimed AVD. Select English if language setup appears. Use only synthetic values: name `Artemis QA`, date `2000-01-01`, time `12:00 PM`, and birthplace `London`. Do not use a real person's profile, account, address, photo, or other personal data. Deny location, notification, and camera permission prompts; the tested screens do not require those permissions. Do not start a camera flow, purchase, subscription, or sign-in.

Preserve screenshots and the Artemis trace before cleanup. Clear only this app's fixture data, stop only processes owned by the accepted run, and return cleanup and release evidence to the Android device squad leader. If cleanup or release cannot be verified, report BLOCKED and leave the target unavailable with a named recovery owner.

## Journey

Use the pinned APK and runner in one bounded Artemis run. Navigate by visible text or accessibility labels, not guessed screen coordinates. If a required control has no stable semantic selector, stop that route and report it as unreachable with the observed reason. Dismiss system permission prompts without granting access.

1. Launch the app from the clean fixture. Complete language and onboarding with English and the Horoscope feature. Enter the synthetic profile values. On the profile date/time pages, capture the rendered profile form with `ProfileNumberPickerView` visible, including its selected and adjacent values. Open birthplace search, search for `London`, select a matching city result, and capture `activity_address_search.xml`. Finish the synthetic profile to reach Home.
2. From Home, open Read Chart and capture `activity_personalized_chart.xml` after the screen settles. A service error or empty terminal state is BLOCKED; it is not a rendering pass.
3. Open the Face Reading tab, then its information screen. Enter `Artemis QA` if the name is empty. Open the date field and capture the date-picker dialog with `WheelColumnView` selected and adjacent values visible. Capture `activity_face_reading_info.xml` separately. Do not continue to the camera.
4. Open the Tarot tab and capture `fragment_tarot_home.xml`. Open the premium screen from its crown, capture it without selecting a purchase or restore action, then return.
5. Open the Tarot library. Capture the populated list and one visible card detail screen. Return to Tarot, open Start Reading, enter a synthetic question (`How can I make a thoughtful choice today?`), and continue through the normal card selection flow. Capture the result only after its sections render. A service error or empty terminal state is BLOCKED; do not retry or infer a pass.
6. Return to Tarot and open the Destiny/Book Guide. Capture the guide with all visible `item_book_step.xml` rows. Continue through the ordinary free flow to Book Open and capture it if the app produces a result. A service error or empty terminal state is BLOCKED; do not retry or infer a pass.
7. After Home has been reached, relaunch the app once through its normal launcher path. Capture the Welcome Back screen while `native_no_media_loading.xml` is visible. If the placeholder is replaced before a stable capture, report that layout as unreachable for this run and include the observed replacement state.

## Assertions and evidence

Capture one screenshot per reached screen before navigating away. Keep each screenshot privacy-safe. One screenshot may cover a parent layout and its visible item layouts on that same screen. Preserve the exact screen-to-screenshot mapping in the report; a model note or run summary alone is not screenshot evidence.

For each captured target, assert that the screen has nonempty visible content, text is legible with no clipped glyphs or text cut off by its parent or viewport edge, and the rendered layout is stable after the screen settles. Allow content below a scroll viewport; do not treat normal scrolling as clipping. On the wheel date picker, inspect the selected Inter 600 value beside unselected Inter 400 values. On the profile picker, inspect visible Inter 400 values. Do not infer font identity from a screenshot if the glyphs are missing or the relevant values are not visible.

Do not retry an application or service failure. Report `PASS` only for observed assertions with their screenshots, `FAIL` only for observed contradictory rendering, `BLOCKED` for an unavailable route, artifact, service, or device, and `NOT-RUN` for any route not attempted. Do not claim that a screen passed because its layout is statically referenced.

## Qualification runs and verdicts

After two independent non-author reviews accept this exact candidate revision and the Android device squad grants an exclusive claim, run three separate attempts against the pinned APK: one positive run, one clean rerun, and one negative control. Use a fresh fixture, `session-id`, `run-id`, and evidence directory for every attempt. The clean rerun repeats the positive goal from the same clean state. Do not retry any attempt. The negative control is a deliberate Checker assertion failure, not an application failure.

Use the `pro` profile with the final Checker enabled and midway checks disabled: `--profile pro --verification-level final`. The final Checker must evaluate the original task goal. Give each run its own `<ledger-root>` and set both `ARTEMIS_TRACES_DIR=<ledger-root>` and `DATA_ENGINE_DB_PATH=<ledger-root>/data_engine.db` so the Checker ledger and native action records share the same evidence root. The expected ledger is `<ledger-root>/<session-id>/check_ledger.jsonl`.

Use these goal and output-description texts for each attempt. Replace the angle-bracket values with the admitted serial, fresh UUIDs, verified APK path, and unique attempt evidence directory. Keep the rendering assertions above identical for all three runs.

Positive goal, used for the first run and the clean rerun:

> Follow the Journey steps using the synthetic fixture. Capture one privacy-safe screenshot for each reached target screen. At every target, verify nonempty visible content, legible text without clipped glyphs or text cut off by its parent or viewport edge, and a stable layout after it settles. On the date picker, verify the selected Inter 600 value and adjacent unselected Inter 400 values. On the profile picker, verify visible Inter 400 values. Report each route and screenshot filename, and mark unavailable routes BLOCKED or NOT-RUN with the observed reason.

Positive output description:

> Return the exact route-to-screenshot mapping, the observed rendering assertions for each route, unavailable routes with their reasons, and the completed task status. Do not report PASS for a route without its screenshot and passed Checker assertion.

Negative-control goal:

> Follow the same Journey with the same rendering assertions and capture requirements. Add this deliberately false final assertion verbatim: `The visible screen contains the text MUST_FAIL_CHE3386_<run-id>.` The token is unique to this run and is not part of the app UI. The Checker must pass every rendering assertion and fail this token assertion. Do not change or weaken any rendering assertion to make the control fail.

Negative-control output description:

> Return the route-to-screenshot mapping and Checker assertions. The token assertion must appear in the latest final Checker ledger attempt with status failed. Every other assertion in that attempt must have status passed. Do not treat a completed task status or a generic runner exit as proof that the control worked.

Run each goal with the pinned runner from the Artemis repository root. Use a 900-second wall limit per attempt. The executor supplies the exact serial and verified APK path only after admission:

```bash
SESSION_ID="<fresh-session-uuid>"
RUN_ID="<fresh-run-uuid>"
ARTEMIS_TRACES_DIR="<attempt-evidence-dir>"
test "$SESSION_ID" != "$RUN_ID"
export ARTEMIS_TRACES_DIR
export DATA_ENGINE_DB_PATH="$ARTEMIS_TRACES_DIR/data_engine.db"
timeout --signal=TERM --kill-after=30s 900s uv run artemis run "<positive-or-negative-goal>" \
  --profile pro --verification-level final --standalone \
  --device-serial "<claimed-serial>" --session-id "$SESSION_ID" \
  --run-id "$RUN_ID" --app-path "<verified-apk-path>" \
  --output-description "<matching-output-description>"
```

After each positive run, verify the run report says `completed`, then run the adapter below with `--expect pass`. Exit 0 is a Checker pass; exit 1 is an assertion failure; exit 2 is invalid evidence. After the negative run, run the adapter with `--expect negative-control --token MUST_FAIL_CHE3386_<run-id>`. Exit 1 is the expected negative-control result only when the adapter JSON says `fail_assertion`; exit 0 means the deliberately false assertion passed and the control is invalid; exit 2 means missing, malformed, or unrelated Checker evidence and the control is invalid. The adapter checks only the latest `final#N` attempt in the ledger and requires every non-token assertion in that attempt to pass.

```bash
python qualification/tools/checker_negative_control_exit.py \
  --ledger "$ARTEMIS_TRACES_DIR/$SESSION_ID/check_ledger.jsonl" \
  --expect negative-control --token "MUST_FAIL_CHE3386_$RUN_ID"
```

For either positive attempt, use the same adapter command with `--expect pass` and omit `--token`.

For each attempt, preserve the command, runner exit, completed-task result, adapter JSON and exit, ledger, report, screenshots, and exact session/run IDs in the evidence destination recorded by the execution report. Any absent ledger or failed prerequisite is BLOCKED. A malformed ledger, missing final Checker assertions, unrelated failed assertion, or wrong adapter exit invalidates the attempt; it is not a pass. Run fixture cleanup after each attempt and verify release before the next attempt. This authoring document does not authorize device access or execution.

## Layout coverage

| Layout | Testcase step or unreachable reason |
| --- | --- |
| `activity_address_search.xml` | Step 1, synthetic birthplace search |
| `activity_book_guide.xml` | Step 6, Destiny/Book Guide |
| `activity_book_open.xml` | Step 6, Book Open after the ordinary free flow; service failure is BLOCKED |
| `activity_face_reading_info.xml` | Step 3, Face Reading information |
| `activity_onboarding.xml` | unreachable: no app binding or code reference inflates this layout; the launcher uses `activity_onboard.xml` for `OnboardingActivity` |
| `activity_personalized_chart.xml` | Step 2, Read Chart |
| `activity_premium.xml` | Step 4, premium screen; no purchase or restore action |
| `activity_profile.xml` | Step 1, synthetic profile setup |
| `activity_tarot_card_detail.xml` | Step 5, visible library card detail |
| `activity_tarot_library.xml` | Step 5, Tarot library |
| `activity_tarot_question.xml` | Step 5, Start Reading question form |
| `activity_tarot_result.xml` | Step 5, result after service returns rendered sections; otherwise BLOCKED |
| `fragment_tarot_home.xml` | Step 4, Tarot tab |
| `item_book_step.xml` | Step 6, visible rows in Book Guide |
| `item_premium.xml` | Step 4, visible premium offer row |
| `item_tarot_library_card.xml` | Step 5, visible library card |
| `item_tarot_section.xml` | Step 5, rendered Tarot result sections; otherwise BLOCKED |
| `native_full.xml` | Step 1, included by the onboarding full native-ad card when that card is visible |
| `native_no_media_loading.xml` | Step 7, Welcome Back placeholder on the second launch, if it remains visible for capture |

The four `dialog_wheel_*_picker.xml` layouts are separate picker variants. This testcase directly opens the date variant required for `WheelColumnView`; it does not claim rendering coverage for month/year, time, or year dialogs. The `styles.xml` and preloaded-font entries are resource references, not additional screens.
