# CHE-984 D2 main-thread blocking case

Case ID: `CHE-984-D2-MAIN-THREAD-v1`

Disposition: `AUTHORED / PARTIAL / BLOCKED / NOT-RUN`

Annex: CHE-770 v5, §3, row D2

This revision specifies a bounded diagnostic subset of D2. It preserves the row's requirement for no beacon disk/network work on main, no blocking wait on beacon, and host-scope isolation on beacon cancellation or failure. The selected sampler cannot prove exhaustive absence of waits, and its separate thread-state and stack reads are not atomic. This case does not claim full D2 acceptance, testcase approval, runner qualification, APK lineage, device qualification, or execution.

## Immutable candidate inputs

| Input | Pin | Status and use |
| --- | --- | --- |
| SDK | `tevo-studio/mMonetize` PR 314, head `4e8e95967cecb672abd1e949402914a68bd57c4e`; predecessor PR 312, head `6ac9777bcb4d0c31e8f10af4d437c5c6b857b37c` | Signed source candidate; use only guarded D2 diagnostic seams. Re-pin if the head changes. |
| Sample | `tevo-studio/mMonetize-sample` PR 133, head `0b2e36f319f667ffa04ace7e8095c1befe810898`; predecessor PR 131, head `b50e6d8a65e005b3555934ac3c36b16c7062fb7e` | Signed source candidate; use only D2 diagnostic controls and retained records. Re-pin if the head changes. |
| Artemis | `cheese-work/artemis` base revision `dd94732489f6ef76f1494043d566062d07506bac` | Case authoring base only. `uv run artemis run` is a CLI reference, not an admitted runner. `uv.lock` SHA-256: `fecfe4364bc321e2a77febf0616b878e38f8daacabee6aea345058e9fc523351`. |
| Android fixture dependencies | Sample version catalog SHA-256 `1eff7be3deb50dde6d6afb60c341307ebee75a66a50c8b423fe7edbe61f822dd`; sample Gradle wrapper `9.2.1`, properties SHA-256 `bcdba2d914e721ca13e3e6bdf73ed9a8eb2f1bc5cc12e0a6cca6d977fb6ab52d`; SDK Gradle wrapper `9.2.1`, properties SHA-256 `5529e8117ebea02ad061ab6b568c36ba58bc066e81d81b9080a09a95514f6449` | Reference identities only. No build or APK was run for this case. The sample does not pin all resolved dependencies with a dependency lock or verification metadata. |

The exact CHE-984 Artemis profile, runner runtime, Android action adapter, resolved runner dependencies, and assertion-to-exit mapping are not selected. `qualification/configs/nova.artemis.jsonc` is not selected. Sol must pin and qualify the exact route before execution. Sol must also set a restricted CHE-984 artifact destination and verify its ACL. Exported records and raw diagnostics stay in that private destination; do not commit or attach raw records to a public PR.

| Assertion source at the pinned candidate | Actual observable |
| --- | --- |
| SDK `ads/core/src/main/java/io/mmonetize/ads/core/internal/D1OfflineFixture.kt` | Guarded diagnostic `d2_schedule` values; actual paid-persistence completion advances the fixture clock and requests a check; the schedule seals and reports through an in-memory connection that opens no socket. |
| SDK `ads/core/src/main/java/io/mmonetize/ads/core/internal/D1UsageDiagnostics.kt` and `ads/core/src/main/java/io/mmonetize/ads/core/internal/usage/UsageTrace.kt` | Private `usage_diagnostics/<run>/<pair>-<arm>-runtime.json` journal with run identity, phase, event, monotonic time, Java thread ID, `main_thread`, host activity, outcome and dropped-event count. |
| SDK `ads/core/src/main/java/io/mmonetize/ads/core/internal/usage/UsageBeacon.kt`, `UsageReporter.kt`, `UsageRuntime.kt` and `D2UploadSchedule.kt` | Real process-owned persistence, sealing, reporter and cancellation paths; deterministic `ACK_204`, `RETRY_503`, `IO_FAILURE` and `CANCEL_UPLOAD` schedules. |
| Sample `app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D1FixtureActivity.kt` | `d2_snapshot` retains live host state, upload markers/outcome, schedule-wide byte count, virtual clock and main-thread observations; `d2_cancel_beacon` deinitializes the SDK and records supplied-host state before/after. `d2_detach` cancels the supplied host and is cleanup only. |
| Sample `app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D2MainThreadProbe.kt` | Main-thread StrictMode disk-read, disk-write and network violations, with listener phase, observed time, type and SDK frames. |
| Sample `app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D2WaitSampler.kt` and `app/src/testD1Offline/kotlin/io/mmonetize/sample/d1/D2WaitSamplerTest.kt` | 5 ms sampled main-thread state and beacon-frame observations; the unit classifier is not device evidence or an exhaustive negative control. |

## Preconditions and fixture/reset

Execution is not admitted by this document. Before any later run, Sol must obtain two fresh independent non-author reviews of this exact revision, final-stack source-signing/OER-1 evidence, successor APK lineage, exact runner/profile/adapter/dependency and assertion-exit mapping, and the restricted artifact destination. Sol must also obtain a fresh exclusive claim and scoped grant for an owned X99 recyclable AVD and applicable target-class qualification. No owner-certified or released claim grants device access.

Use only `d6ControlOfflineDebug` and `d6BeaconOfflineDebug` built from the exact source pins after their gates clear. The target must be API 28 or later because `D2MainThreadProbe` requires the StrictMode penalty listener. The fixture requires the debug and offline-fixture guards; it is not release-like qualification. Both variants use `com.example.synthetic.d1offline` and the same debug signer. Clear only this synthetic app's data once before a new `run_id`, while the grant permits it. Do not clear data or uninstall between arms. Replace the control APK with the beacon APK using an in-place reinstall (`adb install -r` or Sol's pinned adapter equivalent), then confirm the baseline records remain. Stop if the package, signer, fixture markers or retained records differ.

1. Generate a fresh `run_id` of 1–40 ASCII letters, digits, `_` or `-`. Use `p01` for the no-beacon control and `p02`–`p05` for the four independent beacon schedules. Use a fresh process and unique fixture directory for every pair/arm. The fixture rejects duplicate records and non-fresh process-owned stores.
2. Start `d6ControlOfflineDebug` with extras `run_id`, `pair_id=p01` and `arm=control`. Omit `d2_schedule`. The probe starts before SDK init. Complete the actual `d1_show` synthetic interstitial journey and wait for the rendered `d1_mock_ad` and `D1_VALID`. Tap `d1_mock_ad` to dismiss the modal, assert that `d1_mock_ad` is absent, then select `d2_cancel_beacon` and `d2_snapshot` once. This arm is a valid no-beacon fixture baseline, not a provider or licensed-ad run.
3. Install `d6BeaconOfflineDebug` in place. For each of `p02`–`p05`, start a fresh process with the matching `pair_id`, `arm=beacon` and exactly one schedule: `ACK_204`, `RETRY_503`, `IO_FAILURE` or `CANCEL_UPLOAD`. Do not send an arbitrary endpoint or unknown schedule.
4. Complete the actual `d1_show` journey in each beacon process. Wait for the actual paid event and `paid_persist/persisted` journal event. Require the fixture clock to advance once, which requests the real seal/report path. For `ACK_204`, `RETRY_503` and `IO_FAILURE`, wait for the schedule's actual transport/result events. For `CANCEL_UPLOAD`, wait for the current run's `local_transport/request` event in the private runtime journal. Then tap `d1_mock_ad` to dismiss the modal and assert that `d1_mock_ad` is absent before selecting `d2_cancel_beacon`. For `CANCEL_UPLOAD`, select cancellation while the local transport is waiting. If the request marker is unavailable or its run identity is wrong, stop; do not infer that an upload started.
5. After `d2_cancel_beacon`, select `d2_snapshot` exactly once for that pair. Capture its displayed record and `filesDir/d2_records/<run>/<pair>-<arm>-live.json`. The record must retain the actual `host_before_beacon_cancel` and `host_after_beacon_cancel` values. Then select `d2_detach` only to cancel the supplied host for cleanup; retain `filesDir/d2_records/<run>/<pair>-<arm>.json` and verify the final host cleanup fields.
6. Export only the exact D1, D2 live/final, and usage-runtime JSON files for this run through Sol's grant-bound route. Include a SHA-256 for each exported file, the runner report, commands, actual assertion output, exit status and cleanup evidence in the restricted CHE-984 destination. No wildcard export. Sol verifies ACL, cleanup and release.

The stable selectors are `d1_status`, `d1_show`, `d1_mock_ad`, `d2_snapshot`, `d2_cancel_beacon`, `d2_detach` and `d2_record`. The activity receives `run_id`, `pair_id`, `arm` and optional `d2_schedule` extras. These references do not select or qualify an Artemis runner.

## Assertions

Preserve the raw `run_id`, pair, arm, schedule, source/APK identities, files, commands, actual assertion output, exit status and cleanup result. Distinguish assertion failure from runner/infrastructure failure. The bounded diagnostic assertions are:

| ID | Observable and assertion | Failure interpretation |
| --- | --- | --- |
| `D2-IDENTITY-01` | Bind each expected file to its exact JSON paths and expected filename. For each D1 raw file `d1_records/<run>/<pair>-<arm>.json`, require `$.schema=1`, `$.fixture=che1191-d1-v1`, `$.run_id=<run>`, `$.pair_id=<pair>` and `$.arm=<arm>`. For each D2 live file `d2_records/<run>/<pair>-<arm>-live.json`, require `$.schema=1`, `$.run_id`, `$.pair_id`, `$.arm`, `$.fault_schedule`, `$.main_thread.schema=1` and `$.main_thread.fixture=che984-d2-main-v1`; match run/pair/arm to the filename and assigned arm. For each D2 cleanup file `d2_records/<run>/<pair>-<arm>.json`, require `$.run_id`, `$.pair_id`, `$.arm`, `$.apk_sha256`, `$.signer_sha256`, `$.main_thread.schema=1` and `$.main_thread.fixture=che984-d2-main-v1`; match run/pair/arm to the filename. Cleanup files have no top-level `schema`, `fixture` or `fault_schedule`; do not require those fields. For each beacon runtime file `usage_diagnostics/<run>/<pair>-<arm>-runtime.json`, require `$.schema=1`, `$.fixture=che984-usage-probes-v1`, `$.run_id`, `$.pair_id`, `$.arm`, `$.dropped` and `$.events`; match run/pair/arm to the filename. Runtime schedule identity is in the matching `local_transport/request` event outcome, not a top-level schedule field. The control live file has `$.fault_schedule=null` and its null-endpoint path produces no beacon runtime journal; require no beacon events for that arm. The four beacon files each name their assigned schedule. | Missing, duplicate, stale, mismatched, wrong-arm, wrong-type or malformed data is invalid, not PASS. |
| `D2-STRICT-01` | Classify each `main_thread.violations[].sdk_frames` entry. Count frames beginning `io.mmonetize.ads.core.internal.usage.` as beacon frames. Report frames beginning `io.mmonetize.ads.core.internal.D1OfflineFixture` separately as fixture frames. A violation with both is beacon-attributable and fails if its type is disk read, disk write or network. Require zero beacon-attributable disk/network violations. Preserve fixture-only and unattributed violations; never relabel them as beacon work or silently discard them. | Fixture `SharedPreferences.commit()` frames are not an SDK-beacon violation by themselves. Any usage frame in the same violation remains attributable. `listener_phase` is delayed diagnostic context, not exact operation timing. |
| `D2-WAIT-01` | Require `$.main_thread.wait_observations.sampling_only=true`, `$.main_thread.wait_observations.period_ms=5`, nonzero `$.main_thread.wait_observations.sample_count`, zero `$.main_thread.wait_observations.dropped`, and observed phase counts for `init`, `endpoint_lookup`, `ad_load`, `ad_show_and_paid` and `beacon_cancel`. Any retained blocking-state sample with an `internal.usage.` frame fails the sampled subset. Preserve first/last sample times and `max_gap_ns`. | A nonempty sample is positive evidence of a beacon-frame wait. Empty samples do not prove no wait. State and stack are read separately and are not atomic. |
| `D2-LOSS-01` | In every D2 live and cleanup record, require a present JSON integer `0` at `$.main_thread.dropped` (StrictMode violation loss) and `$.main_thread.wait_observations.dropped` (wait-sampler loss). In every expected beacon runtime journal, require a present JSON integer `0` at `$.dropped` (SDK trace loss). Reject missing, null, string, fractional, malformed or nonzero counters. The control's absent runtime journal is expected for its null endpoint; it does not waive any D2 sample counter. | Any loss in any of the three channels invalidates the bounded negative evidence; do not interpret missing records or counters as zero. |
| `D2-SCHEDULE-01` | For each beacon schedule, require `upload_started=true`, `request_bytes>0`, and `fixture_clock_ms == 20001 * 86400000`. Require `ACK_204` to record response `204` and outcome `ACKED`; `RETRY_503` to record response `503` and outcome `RETAINED`; `IO_FAILURE` to record injected connection failure and outcome `RETAINED`; and `CANCEL_UPLOAD` to record `upload/cancelled` after the matching `local_transport/request`. | The byte counter covers the whole schedule, not one request. Use it only to establish nonzero schedule activity; do not treat it as an individual request-size or response-size oracle. A missing expected event or outcome fails the schedule evidence. |
| `D2-HOST-01` | In each beacon live record, require `host_before_beacon_cancel=true`, `host_after_beacon_cancel=true` and `host_active=true`. In the later cleanup record, require host active before `d2_detach` and inactive after it. | Beacon deinit must not cancel the supplied host scope. The explicit later `d2_detach` is cleanup and is not evidence of beacon isolation. |
| `D2-TRACE-01` | For beacon arms only, match `$.events[*].phase`, `$.events[*].event`, `$.events[*].thread_id`, `$.events[*].main_thread` and `$.events[*].outcome` to the journal's top-level run/pair/arm. Require actual `paid_persist/persisted`, the matching `local_transport/request`, the expected schedule response/outcome or cancellation, and final beacon-child completion. Require persistence and upload implementation events to report `$.events[*].main_thread=false`. The no-beacon control has no usage runtime journal and must not be required to contain beacon-only events. | `runtime.json` is asynchronously rewritten. A diagnostics-writer failure can leave it stale. Missing, incomplete or inconsistent expected events invalidate the journal; do not treat an older file as current proof. Compare it with the independent sample record. |

These assertions can detect an observed beacon-attributable main-thread violation or sampled beacon wait. They cannot establish exhaustive absence of blocking waits. The source reads thread state and stack separately, so a matching sample is not an atomic scheduler snapshot. D2 remains `PARTIAL / BLOCKED / NOT-RUN` even if every bounded assertion passes. The source exposes only the synthetic interstitial paid journey, not every applicable journey. Do not weaken the annex row or substitute `d2_detach`, elapsed-time measurements, a no-op upload, or a unit test for the missing proof.

## Intentional wrong-assertion control

After the positive `D2-HOST-01` assertion confirms the beacon live record contains `host_after_beacon_cancel=true`, evaluate this deliberately false assertion against that same record:

```text
D2-NC-01: live.host_after_beacon_cancel == false
```

Require the selected Artemis runner to identify `D2-NC-01` as the assertion failure and return nonzero specifically because of that assertion. A zero exit, another failure reason, missing host evidence or runner/infrastructure error invalidates the control. Do not count the control as a positive D2 result. The runner, report adapter and assertion-to-exit mapping are not selected or qualified, so do not execute this control now.

## CHE-770 v5 §3 device-row map

| Annex row | Disposition in this revision |
| --- | --- |
| N1 | `NOT-AUTHORED / NOT-RUN` |
| N2 | `BLOCKED / NOT-AUTHORED / NOT-RUN` — pending Cheese's decision; no compatible non-debug local licensed/control journey is exposed. |
| N3 | `NOT-AUTHORED / NOT-RUN` |
| D1 — endpoint absent, paired init timing and actual offline ad show | `AUTHORED / NOT-RUN` in separate Artemis PR 191; its approved revision keeps the 5 ms median delta, 50 retained raw pairs and valid no-beacon baseline. |
| D1 — DNS/TLS error | `NOT-AUTHORED / NOT-RUN` |
| D1 — 429/503 | `NOT-AUTHORED / NOT-RUN` |
| D1 — 30-second server hang | `NOT-AUTHORED / NOT-RUN` |
| D2 — StrictMode, callback/thread probes and deterministic sealed-upload/failure subset | `AUTHORED / PARTIAL / BLOCKED / NOT-RUN` — this revision; sampling is not exhaustive and state/stack reads are not atomic. |
| D3 | `NOT-AUTHORED / NOT-RUN` |
| D3b | `NOT-AUTHORED / NOT-RUN` |
| D3c | `NOT-AUTHORED / NOT-RUN` |
| D3d | `NOT-AUTHORED / NOT-RUN` |
| D4 | `NOT-AUTHORED / NOT-RUN` |
| D5 | `NOT-AUTHORED / NOT-RUN` |
| D5b | `NOT-AUTHORED / NOT-RUN` |
| D6 — paired beacon/no-beacon ad behavior | `AUTHORED / PARTIAL / BLOCKED / NOT-RUN` in separate Artemis PR 193; one deterministic local-interstitial subset only. |
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

No Artemis runner, test, build, APK, provider, service, device, ADB, emulator, reset, helper, install, launch, qualification, merge or release action was run while authoring this case. No review or readiness is claimed. Fresh independent testcase reviews and all source, runner, APK, claim/grant and qualification gates remain required.
