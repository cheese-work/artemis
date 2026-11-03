# CHE-3386 SA077 Bundled Inter Rendering

This is an authoring candidate for CHE-3385. It is a testcase definition, not execution evidence.

## Pinned inputs

- App repository: `https://github.com/tevo-studio/sa077-astrology-3`.
- App commit: `c4b7f91e304b58f6eaf3d2b6791232d7d97020cf`.
- Package: `com.horoscope.tarotreader.palmistry.facereader`.
- Expected APK SHA-256: `a8d690b6d7707ac1962ff33d7a1c1f0015a86727774f4f4d2a59391668e367c6`.
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

Capture the first attempt and any retry outcome separately. Do not retry an application or service failure. Report `PASS` only for observed assertions with their screenshots, `FAIL` only for observed contradictory rendering, `BLOCKED` for an unavailable route, artifact, service, or device, and `NOT-RUN` for any route not attempted. Do not claim that a screen passed because its layout is statically referenced.

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
