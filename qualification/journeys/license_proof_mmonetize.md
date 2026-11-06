# License proof journey: MMonetize (CHE-3101)

This journey is an authoring artifact for review. It does not run Artemis, build/install an APK, mutate the test service, or prove device behavior. Execute only after two independent non-author reviews accept the exact journey and manifest revision and Android grants the device and service work.

## Pinned inputs

- SDK source: `tevo-studio/mMonetize` PR #336, head `e6202a48d1411ff57410e3dff98899ae019a0c97`.
- Sample source: `tevo-studio/mMonetize-sample` PR #154, head `0b1c085bd7242da6364a303884eb12ae9215b53f`.
- Service source: `tevo-studio/tevo-license-mobile-sdk` PR #7, head `f91c99e1645343819b56fee00ea1adea7aa55367`.
- Service image: `ghcr.io/tevo-studio/tevo-license-mobile-sdk@sha256:c233b81eddfd0dd45b577b31e35597d792e35747f4a4270f4fbcd81a05c78729`.
- Package: `io.mmonetize.sample`. The proof build is `adsTestLicenseProof`, non-debuggable, signed by a disposable CI key.
- Use port `18080` for both sides: start the service with `LICENSE_TEST_PORT=18080` and build the proof app with `-PlicenseProofPort=18080`. The app endpoint is `http://10.0.2.2:18080/v1/check`; `10.0.2.2` works only in an emulator on X99.

## Fixture and identity setup

Use one unique Compose project owned by this task. Set `MULTICA_TASK_ID` to that project, set `LICENSE_IMAGE` to the pinned digest, and set `LICENSE_DATA_DIR` to an unused absolute path for inactive Compose interpolation. Do not start the `prod` profile.

```sh
docker compose -p <task-project> --profile license-test up -d license-test
```

The `license-test` container starts with a fresh tmpfs database and enforces licensing. Its built-in MMonetize rows use placeholder fixture identities and cannot stand in for this APK. Install the pinned proof APK, clear it, and launch once as a bootstrap to create the real package/certificate row. Read the reported row in `/admin`; record the exact package and certificate from that row. Set each requested status through the service's `/admin` UI using the authorized Access session. Do not put Access credentials in the testcase or evidence.

Record the installed APK SHA-256 and signer certificate from the successful CI artifact for the exact SDK/sample source pair before the cases. Do not reuse a prior local APK. For each case, run `adb -s <device_serial> shell pm clear io.mmonetize.sample` and confirm the command succeeds before launch. Keep the fixture DB for the five cases in this suite so its package/certificate status can be changed; reset it by stopping this task's Compose project and starting a fresh project for the next suite.

## Cases

Capture a screenshot, the single `LicenseProof` result line from logcat, exactly one SDK license error for denials, and the matching `/admin` server row for every online case. Record testcase revision, APK SHA-256, device serial, case ID, command exit status, and status readback with the evidence.

1. **approved**: Set this pair to `approved`; clear app data and launch `adsTestLicenseProof`. Assert the result view reads `Licensed`, SDK initialization proceeds, exactly one result line is logged, and the server row remains `approved` with this pair.
2. **pending**: Set the pair to `pending`; clear and launch. Assert initialization is denied, the result view reads `NotLicensed(packageName=io.mmonetize.sample, ...)`, exactly one SDK license error and one result line are logged, and the server row remains `pending`.
3. **revoked**: Set the pair to `revoked`; clear and launch. Assert initialization is denied, the result view reads `NotLicensed(packageName=io.mmonetize.sample, ...)`, exactly one SDK license error and one result line are logged, and the server row remains `revoked`.
4. **debug-pending**: Use a separate debuggable APK with the same endpoint and a pending row for its own package/certificate pair. Clear its package data and launch. Assert the app initializes, its result view reports the debug license outcome, the server `/admin` row records the same pair with `debuggable=true` and `pending`, and there is one result log. Do not substitute `adsTestLicenseProof`: that build is explicitly non-debuggable. If no reviewed debug APK routes to this emulator endpoint, mark this case NOT-RUN and return the exact build limitation; do not change the SDK or sample in this testcase PR.
5. **offline-no-cache**: Set no cache by clearing app data; stop only this task's service with `docker compose -p <task-project> --profile license-test stop license-test`; launch the proof app. Assert the result view reads `Unverified(...)`, the app remains usable and SDK initialization follows the fail-open path. Record the refused network request, service stop, and absence of a service request row for this case. This is last because the next service start creates a new empty tmpfs database.

## Cleanup

Preserve the evidence before cleanup. Stop only the named task project:

```sh
docker compose -p <task-project> --profile license-test down
```

Confirm the project containers and network are removed. Clear app data after the final case. Do not touch another Compose project, a production database, or an unowned device.
