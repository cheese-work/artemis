# License proof journey: MMonetize (CHE-3101)

This journey is an authoring artifact. It does not execute Artemis, build or install an APK, mutate the service, or prove device behavior. Execute only after two independent reviews accept this journey and manifest revision and the Android device leader grants the exact device and service work.

## Pinned inputs

- SDK source: `tevo-studio/mMonetize` PR #336, head `e6202a48d1411ff57410e3dff98899ae019a0c97`.
- Sample source: `tevo-studio/mMonetize-sample` PR #154, head `0b1c085bd7242da6364a303884eb12ae9215b53f`.
- Service source: `tevo-studio/tevo-license-mobile-sdk` PR #7, head `f91c99e1645343819b56fee00ea1adea7aa55367`.
- Service image: `ghcr.io/tevo-studio/tevo-license-mobile-sdk@sha256:c233b81eddfd0dd45b577b31e35597d792e35747f4a4270f4fbcd81a05c78729`.
- Package and variant: `io.mmonetize.sample`, `adsTestLicenseProof`, non-debuggable.
- APK SHA-256: `56f98069b48aa4a861865cdd4c8be60a006faffa25e261ed8ed41c3ebd22724a`.
- Signer certificate SHA-256: `933d7c0697b33cd8672b1ebfe795d891f898e7ba4a8ac2c4afe86a39fe19b8ce`.
- APK artifact: X99 workflow run `https://github.com/tevo-studio/mMonetize/actions/runs/42023273884`, artifact `license-proof-42023273884-1`. Its receipt pins SDK, sample and license-source SHAs, APK hash, signer and `http://10.0.2.2:8080`.
- Build command: `LICENSE_PROOF_PORT=8080 bash sample/.github/scripts/build-license-proof.sh <mMonetize-SDK-checkout>`.
- Use port `8080` for the service and app. The endpoint is `http://10.0.2.2:8080/v1/check`. `10.0.2.2` works only in an emulator on X99.

## Fixture and identity setup

The Android leader must grant a task-owned X99 AVD claim with its exact serial, permitted actions, evidence destination and cleanup owner before any ADB command. Do not use the X99 AVD until that grant exists.

Use one unique Compose project owned by this task. Set `MULTICA_TASK_ID` to that project, `LICENSE_IMAGE` to the pinned digest and `LICENSE_DATA_DIR` to an unused absolute path for inactive Compose interpolation. Do not start the `prod` profile.

```sh
LICENSE_TEST_PORT=8080 docker compose -p <task-project> --profile license-test up -d license-test
```

The service creates a fresh tmpfs database. Its pre-seeded MMonetize rows use placeholder identities and cannot stand in for this APK. Install this exact APK and certificate, then clear its data and launch once to create the real package/certificate row. The new row is `pending`; use it for the first case before changing its status.

The local admin listener is loopback-only. `AdminAccess` checks only that `Cf-Access-Jwt-Assertion` is nonempty; it does not validate a JWT. For this isolated `license-test` profile, use the literal local marker `license-test-local` only while the listener is bound to `127.0.0.1`. Never use this marker with the `prod` profile or an exposed listener. The service also requires `Origin: http://127.0.0.1:8080` on POST. Use the exact `sdk`, `package`, `cert` and `status` form fields returned for this APK. `LicenseStore.changeStatus` supports `approved` and `revoked`; it cannot change a pair back to `pending`.

Read the admin rows and submit a status change with these loopback requests:

```sh
curl --fail-with-body -H 'Cf-Access-Jwt-Assertion: license-test-local' http://127.0.0.1:8080/admin?status=pending
curl --fail-with-body -X POST -H 'Cf-Access-Jwt-Assertion: license-test-local' -H 'Origin: http://127.0.0.1:8080' -H 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'sdk=mmonetize' --data-urlencode 'package=<package>' --data-urlencode 'cert=<cert_sha256>' --data-urlencode 'status=approved' http://127.0.0.1:8080/admin/status
```

Before each case, run `adb -s <device_serial> shell pm clear io.mmonetize.sample` and confirm success. Uninstall an older APK before installing this disposable signer. Verify the installed APK SHA and certificate with `apksigner` before starting the suite.

For each launch, read the package PID after launch and capture only that process with `adb -s <device_serial> logcat --pid <launch_pid> -v epoch`. Do not count earlier process output. Wait up to 12 seconds for the result view to leave `Checking license`; fail on timeout. For each online case, record the row's `last_seen` before launch and after the result. Require the after value to be strictly later. An unchanged or missing value is inconclusive and cannot pass.

The `/admin` result view can be read with the same header. A status POST must include that header and the exact Origin above. Keep the header value out of command logs and evidence.

## Cases

Record the screenshot, `LicenseProof` result line, SDK denial line where required, server row, launch PID, testcase revision, APK SHA-256, device serial, command exit statuses and before/after server `last_seen` for each case. Apply `message_regex_by_case` from the manifest to the log message, not the full logcat prefix. Approved and debug-pending use `(?<!Not)Licensed`, which does not match `NotLicensed`. Pending and revoked use `NotLicensed\\(packageName=io\\.mmonetize\\.sample`. Offline uses `Unverified\\(reason=`. The result line is tag `LicenseProof`, level `INFO`. The denial match is tag `MMonetize`, level `ERROR`, message containing `Not licensed: package=`. An initialization exception uses tag `LicenseProof`, level `ERROR`, and is not an SDK denial line.

1. **pending**: Use the fresh bootstrap row without changing its status. Clear app data and launch. Assert the view reads `NotLicensed(packageName=io.mmonetize.sample, ...)`, exactly one matching SDK denial line appears for this launch PID, and the online server row remains `pending` with a strictly later `last_seen`.
2. **approved**: Change the pair to `approved` through the local `/admin/status` POST. Read the row back, clear app data and launch. Assert the view reads `Licensed`, zero matching SDK denial lines appear, and the online server row remains `approved` with a strictly later `last_seen`.
3. **revoked**: Change the pair to `revoked`, read it back, clear app data and launch. Assert the view reads `NotLicensed(packageName=io.mmonetize.sample, ...)`, exactly one matching SDK denial line appears for this launch PID, and the online row remains `revoked` with a strictly later `last_seen`.
4. **debug-pending**: The case stays NOT-RUN. The pinned client returns `Licensed` for a debuggable app without contacting the service, so this launch cannot create a new server row or satisfy the server-seen acceptance criterion. The compatible APK is also not pinned. Do not claim a server observation for this case or substitute `adsTestLicenseProof`; it is non-debuggable.
5. **offline-no-cache**: This is last. Clear app data, record the service state, then stop only this task's `license-test` service and launch the proof APK. Assert the view reads `Unverified` with a transport-failure reason and the app remains usable. Record the per-launch result and service stop. Do not claim that an absent row proves no request; the database is tmpfs and disappears on restart.

## Initialization coverage

The pinned sample exposes the typed license result and logs initialization exceptions. It has no success observable that proves MMonetize initialization proceeded or was denied. These cases therefore prove the typed result only; initialization behavior remains NOT-PROVEN. CHE-3101 owns this acceptance gap before the testcase can be accepted as proving initialization.

## Cleanup

Preserve evidence before cleanup. Stop only the named task project:

```sh
docker compose -p <task-project> --profile license-test down
```

Confirm the project containers and network are removed. Clear app data after the final case. Return the AVD claim with cleanup evidence to the Android leader. Do not touch another Compose project, a production database or an unowned device.
