# CHE-5967 X99 AVD Artemis qualification

This is an authoring candidate only. Its receipt is
`qualification/candidates/che5967_x99_avd.v1.json`. The candidate is
provisional: do not execute it until two independent non-author reviews accept
the same receipt and journey digests and Android records an exclusive AVD
admission with an exact serial, permitted actions, evidence destination, and
cleanup owner.

The testcase pins the `adsTestDebug` build from CHE-2705. It launches only the
sample catalog and does not open an ad, billing, or notification flow. The
authoring run used no device, AVD, Artemis invocation, provider call, or app
installation. Execution evidence: NOT-RUN.

## Pinned inputs

- Runner repository: `https://github.com/cheese-work/artemis`.
- Runner commit: `676320699514778d7b589783cb5590ada8d4048f`.
- Sample repository: `https://github.com/tevo-studio/mMonetize-sample`.
- Sample commit: `649ae000ab7527105b6be5351f2bc536f9980bac`.
- App build: `CHE-2705-649ae000ab`, variant `adsTestDebug`.
- Package: `io.mmonetize.sample`, version code `1`.
- APK SHA-256: `38a9b0c53917c5cbf24f78625ac06d6e4a08e1bb1a8c0ac688c663884909f082`.
- Expected device class: `x99_api35_medium_phone`, Google APIs, API 35,
  x86_64, system image revision 9. These are the recorded class attributes,
  not a live-device identity check. The Android executor must verify the image
  and record the runtime-assigned serial before any device command.

## Admission and setup

1. The Android authority must first confirm the exact AVD identity, exclusive
   capacity-one admission, allowed install/launch/cleanup actions, executor,
   evidence destination, and cleanup owner. A historical AVD name, serial,
   process absence, or expired lease is not admission.
2. Both independent non-author reviewers must accept this exact candidate
   before execution. Record their verdicts against the receipt and journey
   digests. Any material edit requires both reviews again.
3. The executor records the granted serial as `SERIAL` and obtains the
   run-bound lease before ADB. Confirm the lease is for that serial and this
   run. Never use another device or reset shared ADB.
4. Confirm the retained APK path from CHE-2705 build evidence. Before each
   run, require its SHA-256 to equal the pinned value above. If the artifact is
   missing or mismatched, stop; do not rebuild or substitute another APK.
5. Create a new evidence directory and fresh, distinct UUIDv4 values for
   `SESSION_ID` and `RUN_ID` for each invocation. Keep each run's output in a
   separate directory. Do not reuse IDs.

The following host-side checks are examples for the executor after admission;
the author did not run them:

```bash
set -euo pipefail
test "$(sha256sum -- "$APK_PATH" | cut -d ' ' -f1)" = \
  "38a9b0c53917c5cbf24f78625ac06d6e4a08e1bb1a8c0ac688c663884909f082"
test -n "$SERIAL"
test "$SESSION_ID" != "$RUN_ID"
```

Before every invocation, clear only `io.mmonetize.sample` on the admitted
serial. Do not clear shared data, other packages, or another run's evidence.
Install only the verified APK through Artemis `--app-path`. Run from the pinned
Artemis commit in standalone Pro mode with native assertion exit enabled,
explicit serial, locked package, unique IDs, a run-specific trace path, and a
run-specific test name. Do not use fake mode or substitute a daemon worker.

## Positive pass and clean rerun

Run the following scenario twice, with a fresh app-data clear, clean evidence
directory, and new UUIDv4 pair before each invocation. The first is the clean
positive pass; the second is the clean positive rerun.

> Launch the app. Verify that the catalog title reads exactly `mMonetize
> Sample`. Scroll the catalog to `XML / View System`. Verify that the
> `XML — Ad Type Showcase` entry is visible. Do not open the entry. Report the
> exact observed title and labels.

Use the public CLI flags `--standalone --profile pro --assertion-exit
--device-serial "$SERIAL" --locked-app io.mmonetize.sample --app-path
"$APK_PATH" --session-id "$SESSION_ID" --run-id "$RUN_ID" --traces-path
"$RUN_EVIDENCE/traces" --test-name che5967-positive` with the goal above.
Set `ARTEMIS_TRACES_DIR` and `DATA_ENGINE_DB_PATH` to paths inside that run's
evidence directory so the native checker ledger and data-engine records are
retained with the trace bundle.

Each positive run passes only when all three exact text labels are observed in
the target app, the native test summary is complete with at least one passed
assertion and no failed, inconclusive, unchecked, or retired assertions, and
the process exits 0. A launch, install, completed orchestration status,
model-reported success, or screenshot without matching native assertion
evidence is insufficient. The second positive run is the clean rerun; it must
pass independently with its own reset, IDs, evidence directory, and exit 0.

## Deliberately false assertion

After the two positive runs, reset only the same admitted package and run one
negative control with fresh IDs and a separate evidence directory. Use the
same pinned APK and exact positive observations, then add this final assertion:

> The catalog title must read exactly `mMonetize Sample — NOT PRESENT`.

The visible title proves the assertion false. The native test summary must be
complete and record that assertion as failed with evidence of the observed
title. `--assertion-exit` must print `ARTEMIS_ASSERTION_EXIT=1`, and the
process exit must equal exactly `1`. Exit `0` means the negative control did
not detect the false assertion. Exit `2`, a missing marker, incomplete
evidence, an infrastructure error, or an unrelated assertion failure makes
the negative control invalid; it is not a successful deliberate failure.

## Retained evidence and cleanup

For both positive runs and the negative control, retain the exact command,
runner commit, testcase receipt and journey digests, app commit/build/package,
APK SHA-256, AVD class and serial, session/run IDs, stdout/stderr, process exit
status, native test summary, trace bundle, checker ledger, data-engine database,
and privacy-safe screenshots or accessibility hierarchy supporting each
assertion. Keep the negative-control result separate from the positive runs.

Preserve artifacts before cleanup. After all artifacts are preserved, stop only this run's Artemis process and
force-stop only `io.mmonetize.sample` on the admitted serial. Verify the app
process is stopped, the AVD is responsive, no run-owned process remains, and
record verified lease release through the admitted cleanup route. Return
cleanup evidence to the Android authority. Do not stop shared ADB or another
owner's process. If release or cleanup is uncertain, keep the AVD unavailable
and name the recovery owner; never infer release from an empty process list.

The executor reports each run as PASS, FAIL, BLOCKED, or NOT-RUN and attaches
its evidence. The Android authority verifies the exact identities, both clean
positive results, the deliberate nonzero assertion result, artifact retention,
and cleanup/release. This testcase contains no accepted execution evidence.
