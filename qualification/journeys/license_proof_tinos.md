# License proof journey: Tinos (CHE-3101)

This journey is an authoring artifact. It does not execute Artemis, build or install an APK, mutate the service, or prove device behavior. Execute only after two independent reviews accept this journey and manifest revision and the Android device leader grants the exact device and service work.

## Pinned inputs

- Sample and SDK source: `tevo-studio/tinos-mobile` commit `ee151046c072e39f127b1e7b0c4c17edc9dce570`. The 5 feature-file blobs at PR #14 head `a2e32dccd9db686459a822d1f3b694e597da6cd3` are identical to this commit (signed review `01a1b9ce-63c2-7dc8-b48a-d70f4e15dbe7`). If PR #14 changes before merge, re-pin.
- Service source: `tevo-studio/tevo-license-mobile-sdk` PR #7, head `f91c99e1645343819b56fee00ea1adea7aa55367`.
- Service image: `ghcr.io/tevo-studio/tevo-license-mobile-sdk@sha256:cfcf54ffbc989049d85bca3e74677cd4e42aca2ed8c5b244f5ba4c066b9d2d8b`.
- Package and variant: `digital.tinos.sample.kotlin`, `licenseProof`, non-debuggable.
- APK SHA-256: `739a0711e14a828546372e15a1f4fd4b7c5db2bd1a0c106429781e495933e6c1`.
- Signer certificate SHA-256: `75c03eae7a89f96434fc3a8b580525a595be1fc41cb5ebda4dcd269710934aaf`.
- APK binary: CHE-4387 attachments `CHE-4387-tinos-licenseProof-ee151046.apk.part01` to `part10`. Concatenate them in order, then verify size 6922004, the SHA-256 above and the `apksigner` signer above against `CHE-4387-artifact-receipt.json` before any device action. Do not rebuild the APK: a rebuild generates a new signing key and a different hash.
- Build command: `bash ./gradlew :tinos-android:testLicenseProofUnitTest :sample-kotlin:assembleLicenseProof -PtevoLicenseRepository=<local-S6-artifact> -PlicenseProofPort=8080 --no-daemon --console=plain --max-workers=2`.
- Use port `8080` for the service and app. The endpoint is `http://10.0.2.2:8080/v1/check`. `10.0.2.2` works only in an emulator on X99.

## Fixture and identity setup

The Android leader must grant a task-owned X99 AVD claim with its exact serial, permitted actions, evidence destination and cleanup owner before any ADB command. Do not use the X99 AVD until that grant exists.

Use one unique Compose project owned by this task. Set `MULTICA_TASK_ID` to that project, `LICENSE_IMAGE` to the pinned digest and `LICENSE_DATA_DIR` to an unused absolute path for inactive Compose interpolation. Do not start the `prod` profile.

```sh
LICENSE_TEST_PORT=8080 docker compose -p <task-project> --profile license-test up -d license-test
```

The service creates a fresh tmpfs database. Tinos rows are not pre-seeded. Install the exact package/certificate pair, clear app data and launch once to create its real row. The new row is `pending`; use it for the first case before changing its status.

The local admin listener is loopback-only. `AdminAccess` checks only that `Cf-Access-Jwt-Assertion` is nonempty; it does not validate a JWT. For this isolated `license-test` profile, use the literal local marker `license-test-local` only while the listener is bound to `127.0.0.1`. Never use this marker with the `prod` profile or an exposed listener. The service also requires `Origin: http://127.0.0.1:8080` on POST. Use the exact `sdk`, `package`, `cert` and `status` form fields returned for this APK. `LicenseStore.changeStatus` supports `approved` and `revoked`; it cannot change a pair back to `pending`.

Read the admin rows and submit a status change with these loopback requests:

```sh
curl --fail-with-body -H 'Cf-Access-Jwt-Assertion: license-test-local' http://127.0.0.1:8080/admin?status=pending
curl --fail-with-body -X POST -H 'Cf-Access-Jwt-Assertion: license-test-local' -H 'Origin: http://127.0.0.1:8080' -H 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'sdk=tinos' --data-urlencode 'package=<package>' --data-urlencode 'cert=<cert_sha256>' --data-urlencode 'status=approved' http://127.0.0.1:8080/admin/status
```

Before each case, run `adb -s <device_serial> shell pm clear digital.tinos.sample.kotlin` and confirm success. Uninstall an older APK before installing this disposable signer. Verify the installed APK SHA and certificate with `apksigner` before starting the suite.

For each launch, read the package PID after launch and capture only that process with `adb -s <device_serial> logcat --pid <launch_pid> -v epoch`. Do not count earlier process output. Wait up to 12 seconds for this case's terminal result: poll the launch-PID log until a `message_regex_by_case` match for the case appears. The in-progress view text (`LicenseResult: checking`) is already present before the network result arrives and never counts; fail on timeout. Bound every log capture: run `adb -s <device_serial> logcat --pid <launch_pid> -v epoch -d` after the wait ends so the command exits by itself, or wrap a streaming capture in `timeout 15`. Never leave an unbounded `logcat`. For each online case, record the row's `last_seen` before launch, then wait until the wall clock passes the next whole second before you launch. `/admin` renders `last_seen` to whole seconds, so this boundary makes a new request show a strictly later value. Record `last_seen` again after the result. Require the after value to be strictly later. An unchanged or missing value is inconclusive and cannot pass.

The `/admin` result view can be read with the same header. A status POST must include that header and the exact Origin above. Keep the header value out of command logs and evidence.

## Cases

Record the screenshot, `TinosSample` result line, SDK denial line where required, server row, launch PID, testcase revision, APK SHA-256, device serial, command exit statuses and before/after server `last_seen` for each case. Apply `message_regex_by_case` from the manifest to the log message, not the full logcat prefix. Approved and debug-pending use `LicenseResult\\.Licensed`. Pending and revoked use `LicenseResult\\.NotLicensed: digital\\.tinos\\.sample\\.kotlin`. Offline uses `LicenseResult\\.Unverified:`. The result line is tag `TinosSample`, level `INFO`. The denial match is tag `Tinos`, level `WARN`, message containing `Not licensed`. The proof transport logs `Proof license request failed: ConnectException` with tag `Tinos` and level `WARN` when the local service connection is refused.

1. **pending**: Use the fresh bootstrap row without changing its status. Clear app data and launch. Assert the view reads `LicenseResult.NotLicensed: digital.tinos.sample.kotlin`, exactly one matching SDK denial line appears for this launch PID, and the online server row remains `pending` with a strictly later `last_seen`.
2. **approved**: Change the pair to `approved` through the local `/admin/status` POST. Read the row back, clear app data and launch. Assert the view reads `LicenseResult.Licensed`, zero matching SDK denial lines appear, and the online server row remains `approved` with a strictly later `last_seen`.
3. **revoked**: Change the pair to `revoked`, read it back, clear app data and launch. Assert the view reads `LicenseResult.NotLicensed: digital.tinos.sample.kotlin`, exactly one matching SDK denial line appears for this launch PID, and the online row remains `revoked` with a strictly later `last_seen`.
4. **debug-pending**: The case stays NOT-RUN. The pinned client returns `LicenseResult.Licensed` for a debuggable app without contacting the service, so this launch cannot create a new server row or satisfy the server-seen acceptance criterion. The compatible APK is also not pinned. Do not claim a server observation for this case or substitute `licenseProof`; it is non-debuggable.
5. **offline-no-cache**: This is last. Clear app data, record the service state, then stop only this task's `license-test` service and launch the proof APK. Assert the view reads `LicenseResult.Unverified` with a transport-failure reason, the launch PID log contains `Proof license request failed: ConnectException`, and the app remains usable. Record the per-launch result and service stop. Do not claim that an absent row proves no request; the database is tmpfs and disappears on restart.

## Initialization coverage

The pinned sample publishes the typed license callback result, but that callback does not prove Tinos runtime construction succeeded or was denied. These cases therefore prove the typed result only; initialization behavior remains NOT-PROVEN. CHE-3101 owns this acceptance gap before the testcase can be accepted as proving initialization.

## Cleanup

Preserve evidence before cleanup. Stop only the named task project:

```sh
docker compose -p <task-project> --profile license-test down
```

Confirm the project containers and network are removed. Clear app data after the final case. Return the AVD claim with cleanup evidence to the Android leader. Do not touch another Compose project, a production database or an unowned device.
