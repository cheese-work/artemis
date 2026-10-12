## CHE-984 D1 endpoint-absent paired init and offline ad case

### Revision and scope

- Case ID: `CHE-984-D1-ENDPOINT-ABSENT-v1`.
- Status: `AUTHORED / NOT-RUN`; this document is not a testcase review, execution result, qualification, or readiness receipt.
- Contract: retain the 5 ms absolute median paired-init delta budget, all 50 raw pairs (100 records), the endpoint-absent no-beacon control, and a real visible offline mock-ad render in both arms.
- Source pair: SDK `tevo-studio/mMonetize` PR 305 at `c0a0305fbe1cefac353021e437b020c5690ae7e5`, based on `922ee1d538f758b7f570d18fbd9155fc971e898f`; sample PR 126 at `4fdc1852b88dd44c2be3889858de93b29e61a1cb`, based on `c2fc577c76f4537d4c5b6a2e15203c16d1205674`.
- The previously recorded APK hash `c65473858bbaf3a837b76e98b532d7b4dfcd8c4d5e0289082de236d4973bdd6b` covers the earlier source pair only. It is not a valid artifact for these successor heads. No successor APK identity is available in this case.

This case covers only the endpoint-absent D1 slice and its required paired timing and ad-render observations. It does not exercise endpoint faults or other CHE-770 rows.

### Runner and dependency references

- Primary lane: Artemis. Canonical fork reference: `cheese-work/artemis` revision `85685763b6399a2ea150b712faab9e5ba7f2bb4d`; repository command entry is `uv run artemis run`.
- Artemis dependency reference: `pyproject.toml` requires Python `>=3.12`; `uv.lock` at that fork revision has SHA-256 `fecfe4364bc321e2a77febf0616b878e38f8daacabee6aea345058e9fc523351`.
- Configuration reference only: `qualification/configs/nova.artemis.jsonc` at that fork revision has SHA-256 `f191cd671d6d0d748640386c912bbce98c708a2057c0af74d35277fe1aceff73`. It is not selected for this case.
- The existing pocket-actual manifest's runner pin `feefd94c72dbce0fa1afc109ffe3d8254f565d6b` is reference-only and is not imported or promoted for CHE-984.
- Sample dependency references: the exact sample source head pins `gradle/libs.versions.toml` (SHA-256 `1eff7be3deb50dde6d6afb60c341307ebee75a66a50c8b423fe7edbe61f822dd`) and Gradle wrapper `9.2.1` (`gradle/wrapper/gradle-wrapper.properties`, SHA-256 `bcdba2d914e721ca13e3e6bdf73ed9a8eb2f1bc5cc12e0a6cca6d977fb6ab52d`). The SDK source head also pins Gradle wrapper `9.2.1`.
- Runner/configuration admission is unresolved: no CHE-984-specific Artemis profile, offline action adapter, or exact X99 runner runtime identity has been selected and qualified. The sample source has no Gradle dependency lock or verification-metadata file; the successor APK's build receipt must preserve the resolved dependency identities and hashes. Do not run `nova`, import another app's runner/configuration, or infer qualification from a source revision.

### Preconditions

Execution is not admitted by this document. Before any later run, Sol must verify two independent non-author reviews of this exact case revision, successor APK lineage for both source heads, the exact compatible runner/configuration/dependency identity, a workspace-restricted evidence destination, a fresh exclusive claim and scoped grant, and applicable target-class qualification. Use only an owned X99 recyclable AVD. No prior release or owner-certified claim grants device access.

The APK must be the opt-in `d1OfflineDebug` fixture build with `-Pd1OfflineFixture=true` and `-PfirebasePerformanceInstrumentationEnabled=false`, built from the pinned SDK/sample heads. The merged manifest must retain the synthetic `com.example.synthetic.d1offline` identity and fixture metadata, and must deny `INTERNET` and automatic providers/services/receivers. A missing or mismatched marker, package, build switch, source head, manifest, or APK hash stops before initialization. Do not use the earlier APK hash as a substitute.

### Fixture and reset

1. Sol records the later run's exclusive target claim, allowed commands, evidence destination, and cleanup owner. Generate one fresh `run_id` of 1–40 ASCII letters, digits, `_`, or `-`; do not reuse a prior run ID. Use every pair ID `p01` through `p50` exactly once.
2. For each arm, pass only `run_id`, `pair_id`, and `arm` (`control` or `beacon`) to `io.mmonetize.sample.d1.D1FixtureActivity` in package `com.example.synthetic.d1offline`. The Activity rejects missing/invalid extras, a reused process, a duplicate record, or a non-fresh per-arm fixture store. Do not pass arbitrary URLs, paths, partner identities, or forwarded intents.
3. Start each arm in a fresh app process. Alternate arm order by pair: control then beacon for odd pair numbers; beacon then control for even pair numbers. Keep the APK, runner, configuration, target image, and instrumentation identical; only `arm` changes. Do not clear app data between arms because it contains the retained raw records. Never reset shared ADB or another app's data.
4. Fixture setup writes only the app's private `d1_fixture` preferences and the per-arm no-backup fixture store. `control` selects a null endpoint. `beacon` selects synthetic `https://d1.invalid/usage`; its URL handler throws before opening a connection. Both arms use the same real `UsageRuntime` attach path. SDK provider bootstrap is suppressed in both arms.
5. Preserve every raw record and failure artifact before cleanup. After verifying the private archive, cleanup may affect only this synthetic package and only within Sol's later grant. Stop only processes owned by that run, verify cleanup, and leave uncertain targets unavailable with a named recovery owner.

### Steps and assertions

Run all 50 pairs. For each arm:

1. Launch the fixture Activity through the later approved Artemis route with the three extras above. Wait for `d1_status` to change from `D1_UNINITIALIZED` to `D1_READY`; `D1_INVALID` or a timeout is a failed/invalid arm, never a pass.
2. Check the recorded arm before showing an ad: `control` must have `attached=false`; `beacon` must have `attached=true`. The control arm is the valid endpoint-absent/no-beacon baseline. The beacon arm is a synthetic timing comparison only; it makes no DNS, TLS, HTTP, or provider request.
3. Activate `d1_show`. Assert that `d1_mock_ad` is present, visible, and has positive on-screen bounds. Dismiss it using its tap-to-dismiss behavior. A status label, callback alone, screenshot alone, successful launch, or exit-zero is insufficient.
4. Wait for `d1_record` and parse its JSON. Require the matching `run_id`, `pair_id`, and `arm`; `fixture="che1191-d1-v1"`; `ad_shown=true`; positive finite `duration_ns == end_ns - start_ns`; `endpoint_complete_ns >= start_ns`; `ad_draw_ns >= end_ns`; and `ad_callback_ns >= ad_draw_ns`. Reject missing, duplicate, malformed, extra-field, mixed-run, or incomplete records. Do not substitute zero for a missing value.
5. Preserve each raw record at `filesDir/d1_records/<run_id>/<pair_id>-<arm>.json`. After all 100 records exist, activate `d1_validate`. Require `D1_PAIRS_PASS` and `abs(median_delta_ms) <= 5.0`; retain the displayed signed median and all raw inputs. `D1_PAIRS_FAIL`, `D1_INVALID_PAIRS`, fewer than 100 records, or any missing pair is not a pass.

Assertion source references are pinned to the sample and SDK heads above:

- `mMonetize-sample/app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D1FixtureActivity.kt:37-43`: stable `contentDescription` selectors `d1_status`, `d1_record`, `d1_show`, and `d1_validate`.
- `D1FixtureActivity.kt:54-76`: exact synthetic input validation, fresh-process guard, monotonic init start/end, asynchronous endpoint completion, and arm-to-`attached` comparison.
- `D1FixtureActivity.kt:77-97` and `:100-128`: actual show callback and raw-record write; show path; 50-pair validation; and the explicit 5 ms result.
- `mMonetize-sample/app/src/d1Offline/kotlin/io/mmonetize/sample/d1/D1Records.kt:5-29` and `:33-45`: schema/invariant validation, exactly 100 records and 50 control/beacon pairs, signed delta calculation, and median calculation.
- `mMonetize-sample/app/src/d1Offline/AndroidManifest.xml:4-29`: synthetic fixture app, removed INTERNET permission and automatic components, and sole fixture Activity.
- `mMonetize/ads/core/src/main/java/io/mmonetize/ads/core/model/D1OfflineInterstitialAd.kt:34-69`: local Dialog render; `d1_mock_ad`; actual shown/attached/nonzero-size draw gate; monotonic draw stamp; and callbacks after draw.
- `mMonetize/ads/core/src/main/java/io/mmonetize/ads/core/internal/D1OfflineFixture.kt:32-74`: opt-in package/build guards, isolated synthetic fixture, arm endpoint choice, and connection-denying `.invalid` handler.
- `mMonetize/ads/core/src/main/java/io/mmonetize/ads/core/MMonetize.kt:121-153` and `mMonetize/ads/core/src/main/java/io/mmonetize/ads/core/internal/usage/UsageRuntime.kt:166-184`: real SDK init/attach path, provider-bootstrap suppression, and endpoint-absent asynchronous completion observation.

#### Intentional wrong-assertion control

After a valid control-arm JSON record has been preserved and parsed, run this deliberately wrong predicate against that record. It must fail because the no-beacon control has `attached=false`, not because of missing input, malformed JSON, runner failure, or infrastructure error:

```sh
python3 -c 'import json,pathlib,sys; record=json.loads(pathlib.Path(sys.argv[1]).read_text()); assert record["attached"] is True, "D1-NC-01: deliberately wrong attached=true expectation; actual=" + repr(record["attached"])' "$CONTROL_RECORD"
```

Expected negative-control result: exit status 1 with `D1-NC-01` and actual `False`. Exit status 0 is a false pass. Any other exception or nonzero result without the expected assertion reason invalidates the negative control. This control does not change the main testcase expectation.

### CHE-770 v5 §3 row map

This maps every device row named for CHE-984. Only `D1 / endpoint absent` is authored here; every other row remains explicitly not authored and not run.

| Annex row | Case disposition |
| --- | --- |
| N1 | `NOT-AUTHORED / NOT-RUN` |
| N2 | `NOT-AUTHORED / NOT-RUN` |
| D1 — endpoint absent, paired init timing, and actual offline ad show | `AUTHORED / NOT-RUN` — this case only |
| D1 — DNS/TLS error, 429/503, and 30-second server hang | `NOT-AUTHORED / NOT-RUN` |
| D2 | `NOT-AUTHORED / NOT-RUN` |
| D3b | `NOT-AUTHORED / NOT-RUN` |
| D3c | `NOT-AUTHORED / NOT-RUN` |
| D5 | `NOT-AUTHORED / NOT-RUN` |
| D5b | `NOT-AUTHORED / NOT-RUN` |
| D6 | `NOT-AUTHORED / NOT-RUN` — its broader paired beacon/no-beacon ad-regression scope is not covered by this D1 case |
| D7 | `NOT-AUTHORED / NOT-RUN` |
| D10 | `NOT-AUTHORED / NOT-RUN` |
| D13 | `NOT-AUTHORED / NOT-RUN` |

### Evidence and limits

- Restricted destination: retain the 100 raw JSON records, negative-control output, runner trace, and failure/cleanup evidence as permission-scoped attachments on CHE-984 in its Multica workspace. Do not put raw records or private identifiers in this public repository or a public PR. Keep a digest and redacted report in the issue; include source/APK/runner/case identities, commands, assertions, outcomes, limitations, reviewer, and cleanup fields. Exclude tokens, headers, raw provider messages, and private identities. Sol must confirm the attachment ACL and exact destination before any run; if the workspace scope cannot be verified, stop.
- The measured interval is foreground `MMonetize.init` duration only. It excludes UI setup/render, record serialization, and asynchronous endpoint/attachment completion; those asynchronous observations remain separately asserted. This case does not accept DNS/TLS/429/503/30-second-hang behavior, real provider behavior, fill rate, or production ad behavior.
- No case runner, test, build, APK, provider, service, device, ADB, emulator, reset, helper, install, launch, qualification, merge, or release action was run while authoring this document. No testcase review, successor APK lineage, runner qualification, fresh claim/grant, or target-class qualification is claimed.
