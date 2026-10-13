## D1 native runner contract

Status: host implementation only. Device qualification and D1 acceptance are
NOT-RUN. This is not a testcase, an execution grant or a replacement for the
immutable reviewed D1 case.

The approved baseline is `f1fab564b820aab72c9005ec1d5dbb2b60cc837f`.
Cheese's D1-only Route decision is recorded on CHE-984 in comment
`01a134b6-220f-7e3e-8300-5aefd10035ba`. The selected primary route remains
the native Artemis Pro graph, Android ADB driver and Checker. No daemon,
fake model, mock driver, cloud device, host-agent transport or alternate
runner is admitted by this profile.

### Configuration and exit mapping

`--qualification che984-d1` requires standalone execution, an explicit
`emulator-<port>` serial, `--profile pro` and the synthetic
`com.example.synthetic.d1offline` package. The profile requires strict Checker
verification and rejects disabled checks. The cosmetic all-device status
probe is skipped. Loaded model nodes and fallback routes must use only
OpenAI `gpt-6.1-sol`. Unset optional judge nodes inherit the configured
Checker model instead of the generic Google default. Explicit unapproved
model overrides fail before Agent initialization. Loaded configuration is
not mutated.

Select `qualification/configs/che984-d1.artemis.jsonc` explicitly through
`ARTEMIS_ARTEMIS_JSONC`; the config contains no credentials. Supply existing
approved credentials through the runtime's normal private environment.
Do not copy credentials into the checkout, logs or artifacts. This profile
does not permit telemetry endpoints, other models, other CHE-984 rows,
physical devices, deployment or new spend.

The SDK's computed native outcome owns the reserved `test_summary` key;
model output cannot supply or override that summary. The opt-in exit adapter
does not infer PASS from completed session status or a trace filename:

| Exit | Required native result |
| --- | --- |
| 0 | Completed task; positive integer passed count; zero failed, inconclusive, unchecked and retired counts; empty failure/retirement lists |
| 1 | Completed task; positive failed count; matching failure items with kind, text and evidence; zero inconclusive, unchecked and retired counts |
| 2 | Missing, malformed, empty, partial, blocked, unchecked, inconclusive or retired assertion evidence |

Cleanup errors propagate as failures. A nonzero exit alone does not qualify
the negative control: the retained native Checker report must identify
`D1-NC-01`, the observed `attached=false` record and the deliberately wrong
`attached=true` expectation. The same route, profile, model, driver and exit
adapter must serve the clean pass, clean rerun and negative control. Host
unit tests use synthetic summaries and do not establish those device results.

### Later qualified invocation

Only the dedicated X99 Luna may invoke the runner after Sol verifies the
published runner head and independent cross-lab review, both exact-case
reviews, immutable APK lineage, configuration/dependency identity, restricted
evidence ACL and a fresh exclusive claim/scoped grant. The first authorized
mutating run may establish the target class qualification. This example is
an invocation contract, not permission to execute:

```sh
ARTEMIS_ARTEMIS_JSONC="$RUNNER_CHECKOUT/qualification/configs/che984-d1.artemis.jsonc" \
ARTEMIS_APP_DIR="$RESTRICTED_STAGE/runner-state" \
uv run --locked artemis run "$REVIEWED_D1_GOAL" \
  --standalone --qualification che984-d1 --profile pro \
  --device-serial "$DEVICE_SERIAL" \
  --locked-app com.example.synthetic.d1offline \
  --test-name "$ATTEMPT_ID" --session-id "$ATTEMPT_ID" \
  --traces-path "$RESTRICTED_STAGE/traces"
```

Pin the runner head, Python/uv identity, lockfile hash, installed dependency
inventory and distribution hashes, model/configuration hash, effective
options, Android driver/hierarchy backend, AVD image/API/ABI and serial.
Record immutable case/source/APK/package/signer identities and restricted
artifact destination. Inspect configured tools and endpoints before admission;
model-route approval does not authorize arbitrary MCP services or traffic.

Preserve reset backups and CHE-819 originals. D1 retains all 50 raw pairs,
the 5 ms absolute median delta, a valid no-beacon baseline, real visible
offline mock-ad rendering and P2/P3 limits. Native outcome counts are not
proof of complete case coverage. Sol separately verifies every raw artifact,
negative-control provenance and cleanup/release. CHE-387's flake baseline
remains a separate acceptance gate. D2/D6 remain outside this profile; N2
remains NOT-RUN by Cheese's decision.
