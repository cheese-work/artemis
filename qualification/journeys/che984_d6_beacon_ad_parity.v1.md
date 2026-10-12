# CHE-984 D6 beacon/no-beacon ad parity case

Case ID: `CHE-984-D6-AD-PARITY-v1`

Disposition: `AUTHORED / PARTIAL / BLOCKED / NOT-RUN`

Annex: CHE-770 v5, §3, row D6

This revision specifies the deterministic local-interstitial comparison that the pinned sample exposes. It does not claim overall D6 acceptance, testcase approval, source clearance, runner qualification, APK lineage, device qualification, or execution.

## Immutable candidate inputs

| Input | Pin | Status and use |
| --- | --- | --- |
| SDK | `tevo-studio/mMonetize` PR 314, head `4e8e95967cecb672abd1e949402914a68bd57c4e`; predecessor PR 312, head `6ac9777bcb4d0c31e8f10af4d437c5c6b857b37c` | Signed source candidate; use only the D6 local offline fixture seams. Re-pin if the head changes. |
| Sample | `tevo-studio/mMonetize-sample` PR 133, head `0b2e36f319f667ffa04ace7e8095c1befe810898`; predecessor PR 131, head `b50e6d8a65e005b3555934ac3c36b16c7062fb7e` | Signed source candidate; use only the D6 offline variants and retained records. Re-pin if the head changes. |
| Artemis | `cheese-work/artemis` base revision `dd94732489f6ef76f1494043d566062d07506bac` | Case authoring base only. Primary CLI entrypoint is `uv run artemis run`; Python requirement is `>=3.12`; `uv.lock` SHA-256 is `fecfe4364bc321e2a77febf0616b878e38f8daacabee6aea345058e9fc523351`. |
| Android fixture dependencies | Sample `gradle/libs.versions.toml` SHA-256 `1eff7be3deb50dde6d6afb60c341307ebee75a66a50c8b423fe7edbe61f822dd`; sample Gradle wrapper `9.2.1`, properties SHA-256 `bcdba2d914e721ca13e3e6bdf73ed9a8eb2f1bc5cc12e0a6cca6d977fb6ab52d`; SDK Gradle wrapper `9.2.1`, properties SHA-256 `5529e8117ebea02ad061ab6b568c36ba58bc066e81d81b9080a09a95514f6449` | Reference identities only. No Gradle task or APK was run for this case. The sample does not pin all resolved dependencies with a dependency lock or verification metadata. |

The exact CHE-984 Artemis profile, Android action adapter, runner runtime identity, dependencies resolved by that runtime, and assertion-to-exit mapping are not selected. `qualification/configs/nova.artemis.jsonc` is not selected for this synthetic case. Sol must pin and qualify a CHE-984-specific route before execution. The CLI entrypoint and lockfile digest above are references, not proof of an admitted runner.

| Assertion source at the pinned candidate | Actual seam used |
| --- | --- |
| SDK `ads/core/src/main/java/io/mmonetize/ads/core/internal/D1OfflineFixture.kt` | The control arm uses a null endpoint. The beacon arm uses the fixture URL handler, which throws before opening a connection. |
| SDK `ads/core/src/main/java/io/mmonetize/ads/core/model/D1OfflineInterstitialAd.kt` | A drawn local dialog updates `ShowInterAdState` with `isAdShowed=true` and `isAdImpression=true`, then dispatches callbacks. |
| Sample `app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D1FixtureActivity.kt` | `getOrFetchInterAd(ALIAS)` result is captured before `showAd`; its show callback checks alias, shown and impression state before retaining the show record. |
| Sample `app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D6AdRecord.kt` and `D6Evidence.kt` | Record parsing checks exact schema and value types. `d6Compare` requires complete unique control/beacon pairs, distinct APK hashes, matching signer and SDK, and reports fill differences as observation-only. |
| Sample `app/src/testD1Offline/kotlin/io/mmonetize/sample/d1/D6RecordsTest.kt` | Host unit assertions cover differing fill observations, missing/duplicate/mixed-run pairs, mislabeled build identity, impossible show state, and malformed JSON. These are source references, not testcase or device results. |

## D6 scope and residual blocker

The deterministic subset uses the real SDK call `MMonetize.InterstitialAdsRepository.getOrFetchInterAd("d1_local_interstitial")`, the returned load result, and the actual `InterstitialAdsPresenter.showAd` callback and mock dialog draw. The control arm supplies a null usage endpoint. The beacon arm supplies only `https://d1.invalid/usage`, whose fixture URL handler throws before opening a connection. The sample config and ad alias are identical in both arms. No provider endpoint or real telemetry is used.

The selected source exposes load/fill records and a separate show/impression record for this one local interstitial. `D6Evidence.d6Compare` returns retained counts with `observation_only: true`; it does not decide acceptance. The source receipt says this seam does not establish the full ad/fault parity surface or real-network variability. That is the exact testability blocker: the available revisions expose no additional ad/fault journey or separate network-variability observable with which to substantiate the complete D6 row. Do not substitute this interstitial-only result for full D6, introduce a provider call, or weaken the row. D6 remains `BLOCKED` pending a verified source seam or a separately approved resolution of the missing observables.

## Preconditions and fixture/reset

Execution is not admitted by this document. Before any later run, Sol must obtain two independent non-author reviews of this exact revision, final-stack source signing and OER-1, successor APK lineage, the exact compatible Artemis runner/profile/action adapter/dependency and assertion-exit mapping, and a restricted evidence destination. Sol must also obtain a fresh exclusive claim and scoped grant for an owned X99 recyclable AVD and applicable target-class qualification. No owner-certified or released claim grants device access.

Use only `d6ControlOfflineDebug` and `d6BeaconOfflineDebug` built from the exact pinned source stack after its gates clear. The fixture requires `BuildConfig.DEBUG && D1_OFFLINE_FIXTURE`; it is not release-like qualification. Both arms must retain package `com.example.synthetic.d1offline`, one current signer, and the same SDK version. Each arm must be a monolithic APK with a different observed APK SHA-256. Verify the fixture manifest has no `INTERNET` permission and no automatic provider/service/receiver. Any changed source pin, package, arm, signer, SDK version, manifest property, split APK, or missing fixture marker stops the run.

1. Sol records the later exclusive claim, exact permitted actions, restricted artifact destination, and cleanup owner. Generate one fresh `run_id` of 1–40 ASCII letters, digits, `_`, or `-`. Use `p01` through `p50` once for each arm. These 50 D6 pairs use the fixture's available ID range; they do not assert or replace D1's separate 5 ms / 50-raw-pair timing contract.
2. Before any data clear, export all earlier D1/D2/D6 evidence that must be retained from this synthetic package to the Sol-approved restricted destination. Verify the exact file set and each file's SHA-256 there before clearing. Then clear only this synthetic app's data once before the new `run_id`, while the granted target and command allow it. Do not clear app data between arms or pairs: `filesDir/d6_records/<run_id>/` must retain both arms for `d6_compare`. Do not use a prior run directory. If earlier evidence cannot be exported and verified, or a clean run cannot be established without deleting another app's data, stop.
3. Run `control` pairs `p01`–`p50` in `d6ControlOfflineDebug`. Then replace the installed APK with `d6BeaconOfflineDebug` through an in-place reinstall (`adb install -r` or Sol's pinned adapter equivalent). Both flavors use `com.example.synthetic.d1offline` and the same debug signer. Do not uninstall, clear app data, or change the run directory between arms. Confirm all control records remain present after the reinstall. If app data does not survive the in-place reinstall, stop; this source exposes no import operation that can safely reconstruct the comparison set.
4. Start a fresh app process for every pair/arm. Pass only the expected `run_id`, `pair_id`, and `arm` extras to `io.mmonetize.sample.d1.D1FixtureActivity`. The activity must reject a duplicate or non-fresh per-arm fixture store. Keep other app, device, runner, target image, and ad configuration values fixed. The named fresh-process command sequence for Sol's later pinned runner/action adapter is:

   ```sh
   adb shell am force-stop com.example.synthetic.d1offline
   adb shell am start -W -n com.example.synthetic.d1offline/io.mmonetize.sample.d1.D1FixtureActivity --es run_id "$RUN_ID" --es pair_id "$PAIR_ID" --es arm "$ARM"
   ```

   Sol must bind and qualify this exact package/component/extra mapping through the selected adapter before execution. This command reference does not authorize execution.
5. Treat `d1_status == D1_READY` only as fixture setup. Select `d1_show`; wait for the actual `d1_mock_ad` view and for the real show/impression callback. Read load and show evidence from the retained `filesDir/d6_records/<run_id>/<pair_id>-<arm>-load.json` and `filesDir/d6_records/<run_id>/<pair_id>-<arm>-show.json` files, not from the `d6_record` UI text. The UI is not the stable source for both records. Dismiss only `d1_mock_ad`, and preserve each record even if a later step fails. `D1_VALID` is not a D6 result.
6. On the final `beacon` / `p50` activity, after its show record is present and the dialog is dismissed, select `d6_compare` without relaunching. Capture `d6_comparison` and `filesDir/d6_records/<run_id>/comparison.json`. A missing, malformed, duplicate, mixed-run, mixed-build, or incomplete record set is a failure or NOT-RUN; it is never a pass.
7. Export only the exact 100 `*-load.json` files, 100 `*-show.json` files and `comparison.json` for this `run_id` by the Sol-granted route. Hash every exported file with SHA-256 and retain the manifest, comparison output, runner report, assertion results, and cleanup evidence as restricted CHE-984 attachments after Sol verifies the ACL. Do not put raw records or private data in this public repository or PR. Do not use wildcard device exports. Sol verifies cleanup and release.

The sample exposes stable selectors `d1_status`, `d1_show`, `d1_mock_ad`, `d6_record`, `d6_compare`, and `d6_comparison`. It receives `run_id`, `pair_id`, and `arm` as activity extras. The runner-specific launch and assertion adapter remain unpinned; these references do not authorize a runner or device command.

## Assertions

The Artemis report must preserve the raw `run_id`, arm, pair, source/APK identities, commands, actual assertion output, exit status, and cleanup result. It must distinguish assertion failure from runner/infrastructure failure. The acceptance predicates for the deterministic subset are:

| ID | Observable and assertion | Failure interpretation |
| --- | --- | --- |
| `D6-LOAD-01` | From the exact exported file bytes, require exactly 100 unique `filesDir/d6_records/<run_id>/<pair_id>-<arm>-load.json` records: one for each `p01`–`p50` × `control`/`beacon`. Verify each file's SHA-256 against the restricted manifest before parsing. Require typed values `$.schema=1`, `$.fixture=che984-d6-local-v1`, the requested `$.run_id`, `$.pair_id`, `$.arm`, `$.stage=load`, `$.load_attempts=1`, `$.fill_denominator=1`, `$.fill_numerator=$.load_successes`, `$.load_successes` in `{0,1}`, `$.load_start_ns > 0` and `$.load_end_ns >= $.load_start_ns`. Do not use the `d6_record` UI as a load-record source. | Missing/extra/invalid files, missing/wrong-type fields, digest mismatch or load errors fail this deterministic subset; do not discard a failed pair. |
| `D6-BUILD-01` | Before execution, Sol's successor APK lineage receipt must bind each exact flavor to its source pins and immutable installed-APK SHA-256. Every control `*-load.json` and `*-show.json` must have `$.build_arm=control` and `$.apk_sha256` equal to the control digest in that receipt; every beacon file must bind `beacon` and its receipt digest. Require `$.package=com.example.synthetic.d1offline`, identical `$.signer_sha256` and `$.sdk_version` across arms, and distinct control/beacon APK digests. Require `comparison.json` `$.control_apk_sha256` and `$.beacon_apk_sha256` to match those same receipt digests. Self-reported hashes without Sol's lineage receipt are insufficient. | Missing lineage, source/pin mismatch or any record/comparison identity mismatch invalidates the pair set; labels and self-reported hashes alone are not build identity. |
| `D6-FILL-01` | Parse `comparison.json` and the exact exported load-file bytes after digest verification. Require `$.observation_only=true`, `$.pairs=50`, `$.control_load_attempts=50`, `$.beacon_load_attempts=50`, `$.control_fills=50`, `$.beacon_fills=50` and `$.differing_fill_pairs=0`. Independently compare every paired raw JSON integer at `$.load_successes`, `$.fill_numerator` and `$.fill_denominator`; require all three fields present, integer-typed and equal to `1` for each control/beacon record. Reject missing, wrong-type or malformed fields rather than comparing absent values. | Any failed fill or control/beacon difference fails the deterministic subset. `observation_only` is never PASS. |
| `D6-SHOW-01` | From the exact exported file bytes, require exactly 100 unique `filesDir/d6_records/<run_id>/<pair_id>-<arm>-show.json` records, one per pair/arm, with typed `$.stage=show` and `$.ad_shown=true`; verify every file SHA-256 against the restricted manifest. Confirm the actual callback requires the fixture alias plus `isAdShowed=true` and `isAdImpression=true`, and the runner observed `d1_mock_ad` before dismissal. Bind each show record's `$.apk_sha256` through `D6-BUILD-01`. | Missing show, impression, rendered dialog, show record, required field or digest fails the deterministic subset. Load counts cannot substitute for show evidence. |

No statistical tolerance or averaging is applied to deterministic pairs. If a failure is observed, preserve it and report it; never use real-network variability to hide it. The D6 row stays blocked even if every listed subset assertion passes, because this source slice does not expose the complete row's ad/fault and network-variability evidence.

## Intentional wrong-assertion control

After the positive subset assertions pass, evaluate this deliberately false assertion against the same retained comparison:

```text
D6-NC-01: comparison.differing_fill_pairs == 1
```

The expected actual value is `0`. Require the selected Artemis runner to report `D6-NC-01` as the assertion failure and return nonzero specifically because of that assertion. A zero exit, another failure reason, invalid source data, or runner/infrastructure error invalidates the control. Do not count this control as a positive D6 pair. The runner, report adapter, and assertion-to-exit mapping are not yet qualified, so do not execute it now.

## CHE-770 v5 §3 device-row map

| Annex row | Disposition in this revision |
| --- | --- |
| N1 | `NOT-AUTHORED / NOT-RUN` |
| N2 | `BLOCKED / NOT-AUTHORED / NOT-RUN` — Sol's separate source-recovery slice is pending; no licensed/control local journey is evidenced here. |
| N3 | `NOT-AUTHORED / NOT-RUN` |
| D1 — endpoint absent, paired init timing, actual offline ad show | `AUTHORED / NOT-RUN` in separate Artemis PR 191; this D6 revision does not alter its 5 ms / 50-raw-pair contract. |
| D1 — DNS/TLS error | `NOT-AUTHORED / NOT-RUN` |
| D1 — 429/503 | `NOT-AUTHORED / NOT-RUN` |
| D1 — 30-second server hang | `NOT-AUTHORED / NOT-RUN` |
| D2 | `AUTHORED / PARTIAL / BLOCKED / NOT-RUN` in separate Artemis D2 case; sampling is not exhaustive and state/stack reads are not atomic. |
| D3 | `NOT-AUTHORED / NOT-RUN` |
| D3b | `NOT-AUTHORED / NOT-RUN` |
| D3c | `NOT-AUTHORED / NOT-RUN` |
| D3d | `NOT-AUTHORED / NOT-RUN` |
| D4 | `NOT-AUTHORED / NOT-RUN` |
| D5 | `NOT-AUTHORED / NOT-RUN` |
| D5b | `NOT-AUTHORED / NOT-RUN` |
| D6 — paired beacon/no-beacon ad behavior | `AUTHORED / PARTIAL / BLOCKED / NOT-RUN` — deterministic local-interstitial assertions only; full D6 remains blocked by the exact testability gap above. |
| D7 | `NOT-AUTHORED / NOT-RUN` |
| D8 | `NOT-AUTHORED / NOT-RUN` |
| D9 | `NOT-AUTHORED / NOT-RUN` |
| D10 | `NOT-AUTHORED / NOT-RUN` |
| D11 | `NOT-AUTHORED / NOT-RUN` |
| D12 | `NOT-AUTHORED / NOT-RUN` |
| D13 | `NOT-AUTHORED / NOT-RUN` |
| D14 | `NOT-AUTHORED / NOT-RUN` |
| D15 | `NOT-AUTHORED / NOT-RUN` |

All non-device CHE-770 annex rows remain outside this device-case revision and `NOT-AUTHORED / NOT-RUN`.

## Run record

No Artemis runner, test, build, APK, provider, service, device, ADB, emulator, reset, helper, install, launch, qualification, merge, or release action was run while authoring this case. No review or readiness is claimed. Two independent reviews of the published immutable case revision and every listed execution gate remain required.
