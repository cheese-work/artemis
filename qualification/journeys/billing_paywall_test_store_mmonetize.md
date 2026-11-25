# Billing Paywall Test Store journey (CHE-9833)

This is a provisional, text-only testcase candidate. It does not run Artemis, build or install an APK, or use a device or ADB. Execution is NOT-RUN and requires approval of this exact journey and manifest revision by two independent non-author reviewers, followed by a separate Android device admission.

## Pinned inputs

- Artemis repository: `https://github.com/cheese-work/artemis`, base `main`.
- Sample repository: `https://github.com/tevo-studio/mMonetize-sample`, merge commit `c8cdf33bae61bb76577f0dacb14604011fe7e83d` (CHE-8973 / PR #180).
- Paired SDK: `https://github.com/tevo-studio/mMonetize`, `release/0.0.28` at `cc2a1cb9b94e7db675cb6826df7b867895a19d4d`.
- Entry: **Paywall — Real offering**, `BillingPaywallDemoActivity`.
- App package: `io.mmonetize.sample`.
- RevenueCat Test Store product: `che801_premium_monthly_test`; entitlement: `premium`; offering: `che801_test`.
- The sample build must have no RevenueCat key for case E. Do not put a key in this repository or in Multica.

## Admission and fixture

Before any device command, the Android device leader must grant the exact target, serial, permitted actions, evidence destination, executor and cleanup owner. The leader selects the device class; this candidate does not claim a device or qualify a runner.

For each case, start from clean app data using `adb -s <device_serial> shell pm clear io.mmonetize.sample`, then open the sample and select **Paywall — Real offering**. `pm clear` clears app data; it is not an APK install. If the package is absent, install only the APK admitted for the execution issue, then establish the clean state before continuing. Do not clear another package or shared device state.

Cases A, B and D need a configured RevenueCat Test Store build and the existing `che801_test` offering. Case C must restore the Test Store purchase made in B. Case E uses the stage 4a build without a RevenueCat key. Never record or expose a key in testcase evidence.

## Cases

For each case, retain a privacy-safe screenshot, the relevant app logcat line, the RevenueCat customer entitlement state, this testcase revision, the app build SHA, the exact device serial and command exit status. Record missing or contradictory evidence as BLOCKED or FAIL; do not infer a pass from a model summary or a screenshot alone.

1. **A — Offering and prices.** With clean app data, open **Paywall — Real offering**. Assert that the plan list contains the packages from offering `che801_test` and displays their Test Store localized prices. Record the visible package names and prices. Do not hard-code a locale-specific price.
2. **B — Purchase.** Select the monthly package and complete the RevenueCat Test Store purchase. Assert that the **Premium active** toast appears and `BillingPaywallDemoActivity` closes. Record the `premium` entitlement as active for the RevenueCat customer.
3. **C — Restore.** After B, clear only `io.mmonetize.sample` app data with `pm clear`, reopen the sample and enter **Paywall — Real offering**. Tap **Restore**. Assert that the result is Active and the sample's `onPurchased` callback fires. Record the `premium` entitlement state returned for this customer.
4. **D — Cancel.** With clean app data, open the real offering, select the monthly package and cancel in the Test Store dialog. Assert that no purchase error message appears and the paywall remains open. Record the `premium` entitlement state as inactive.
5. **E — Billing not configured.** Use the stage 4a build with no RevenueCat key. Open **Paywall — Real offering**. Assert that the **Billing is not configured** message appears. Do not enter a Test Store purchase flow.

## Execution boundary

The executor runs only this reviewed revision, records the actual app build SHA and serial for every case, and preserves the returned evidence before cleanup. Do not change expected results during execution. Any material testcase edit requires both independent reviews again. Artemis execution, device qualification and accepted execution evidence belong to the separate stage 4b-2 issue.
