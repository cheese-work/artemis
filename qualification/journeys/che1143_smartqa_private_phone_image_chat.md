# CHE-1143 SmartQA private-phone and image-chat acceptance

Status: `PROVISIONAL`, `NOT-RUN`. This packet is a testcase specification. It is not device admission, browser acceptance, provider evidence, or a qualification result.

## Pinned inputs

- Artemis runner and SmartQA source revision: `68eab0af80eecb12e5d9fcc05a8e5635b31aff94` from `https://github.com/cheese-work/artemis`.
- The same source revision pins `apps/admin_console` and `apps/showcase_ui`. This is a source pin, not a verified serving revision. Read the served revision before browser acceptance; stop if it differs.
- Python dependencies: `uv.lock`, SHA-256 `fecfe4364bc321e2a77febf0616b878e38f8daacabee6aea345058e9fc523351`.
- Browser dependencies: `apps/showcase_ui/package-lock.json`, SHA-256 `2e4aebe81458df7615678be7353b625bfffb96a0876f11c4db16f03164569874`.
- Synthetic image input: `tests/tools/inputs/screenshot.jpg`, SHA-256 `1e6bbc31b77abf4701b6c54466e4feda1e816af45ba19b78a5af9d90f1ccd891`.
- The immutable testcase revision is the published PR head SHA. The companion candidate receipt pins this journey by digest. Both independent testcase decisions must name that same PR head before execution.

## Existing coverage to reuse

- **BUG-001:** `tests/unit/admin_console/test_device_stream_scope.py` already covers owner/admin visibility, another-QA denial, frame filtering, and shared-phone visibility. `tests/unit/admin_console/test_task_device_readiness.py` and `tests/unit/admin_console/test_run_device_binding.py` cover admission/binding behavior. These are source regressions, not private-browser-phone acceptance.
- **BUG-002:** `tests/unit/admin_console/test_run_ownership.py` covers per-row background-event filtering and owner-only named lifecycle events. `tests/unit/admin_console/test_preview_routes.py::test_status_shows_each_qa_only_their_own_queue` covers synthetic QA queue scope. These are source regressions, not live browser-event evidence.
- **REQ-003:** `apps/showcase_ui/src/app/pages/workspace/workspace-image-chat.spec.ts` covers attach, preview/removal, file-type limits, text-plus-image submission, and retry identity. `tests/unit/admin_console/test_run_images.py` covers accepted formats, rejection, queue payloads, ownership, and worker delivery. These tests do not prove a real provider received the image.
- No test in this packet claims those existing tests passed during authoring.

## Authorized synthetic setup

Use only the existing isolated preview route documented in `docs/preview-fixtures.md`: `ARTEMIS_PREVIEW_PROFILE=1` and `ARTEMIS_PREVIEW_IDENTITY_SWITCH=1`, with the fixed aliases `qa-a`, `qa-b`, and `admin` mapped to `qa-a@example.test`, `qa-b@example.test`, and `admin@example.test`. The preview owner supplies any already-created private preview URL and fixture location. Use separate browser contexts and the preview identity cookie. Do not use real QA accounts, credentials, Cloudflare sign-in, or production data for synthetic checks.

The synthetic preview returns an empty device list and disables device, provider, lifecycle, and real task-execution routes. It cannot prove private-phone isolation, owner-filtered live events, or image delivery to a provider. Do not substitute synthetic or shared-AVD evidence for the private-browser-phone case. No preview is started by this authoring packet.

## Compatible target and admission

- **Target class:** MBP-hosted Chrome with one physical Pixel 6 Pro connected through the owner browser's private phone bridge. Prefer USB; wireless is the backup. Do not use a shared AVD for this coverage.
- **Admission owner:** `x99-codex-sol` selects the dedicated Android-squad executor and records an exclusive claim before any device command. The host identifier, exact phone serial, claim/run owner, claim state, and evidence destination are not supplied in this packet.
- **Identity inputs:** two already-authorized, distinct, non-admin QA identities are required for live acceptance. Record labels only in evidence; never put credentials in the testcase or report.
- **Provider gate:** use no provider until an eligible account owner, provider, and numeric spend scope are recorded. New Anthropic Android runs remain halted. A gateway reachability result is not provider acceptance.
- **Review gate:** two independent non-author reviewers selected under the live Android-squad policy must approve the exact immutable PR head before any execution, including first-class device-class qualification. Material changes require both decisions again.

## Cases

### BUG-001 — Private browser-phone isolation

1. At the verified serving revision, revalidate the two existing QA profiles with read-only `GET /api/system/whoami`; require two distinct regular-QA identities and no admins. Then, after Sol records admission, sign in as QA-A in the browser context that owns the Pixel 6 Pro bridge and QA-B in a separate context. Do not provision accounts or access credentials.
2. In QA-A's context, open `app-workspace-device-chip button[aria-haspopup="true"]`, then use the `button.action` named `Connect a phone from this browser`. Verify the phone appears as the selected option under `[role="group"][aria-label="Phones"] button.option`. Record the exact admitted serial in Sol's claim. Use only an owner-approved synthetic screen; stop if personal content appears.
3. In QA-B's context, open the same chip and inspect the `[role="group"][aria-label="Phones"] button.option` list. Verify QA-B cannot select the exact private serial in Sol's claim. Do not submit or issue any task-control request from QA-B.
4. Read `GET /api/stream/device-state` in each context. Its stream source is the first connected ADB serial, not the selected workspace phone. Require QA-A's response serial to equal the exact serial in Sol's claim. For a private claimed serial, require QA-B's response to be `{"connected":false,"serial":null,"live_stream_url":null}` and verify QA-B receives no frame from that serial. If the response serial differs from Sol's claim, mark this observation `NOT-RUN`; do not reorder ADB or treat the UI selection as stream-source evidence. Confirm the serving revision before treating the observation as live acceptance.
5. The safe pre-enqueue control is the read-only QA-B selector check in step 3: the private serial must be absent and unselectable. Stop before any `/api/run` request. This control proves only UI-level prevention; the server-side target-control contract remains `UNKNOWN` and `NOT-RUN`. Do not send a host-bound request until Sol verifies the serving revision and approves a safe control that proves rejection before enqueue and before any device command. Do not let either identity switch Sol's reviewed host-selected serial to the private bridge serial.
6. Verify an explicitly shared lab phone remains visible to both accounts. Do not alter its sharing state or reset shared ADB.

**Provisional evidence:** paired owner/non-owner browser observations, the exact claim serial, privacy-safe target labels, read-only stream-state responses, and serving revision. This does not cover server-side task-control authorization. Keep that control `NOT-RUN` until the serving contract is verified and a reviewed negative control proves rejection before enqueue and before any device command.

### BUG-002 — Owner-filtered events

1. Use the same two authorized QA contexts. Before QA-A submits anything, open one `GET /api/stream/active` SSE connection in each context. Require HTTP `200`, an open stream, and its initial `info` message `Subscribed to session active` in both contexts. Record both handshakes. If either stream is not healthy, do not submit the run and record the case `NOT-RUN`.
2. After both handshakes, submit the single QA-A run described in REQ-003. Keep both SSE connections open and capture each stream continuously. Require QA-A to receive positive run-scoped evidence for the exact run, including `session_started` and `session_ended`. Keep QA-B's capture through QA-A's `session_ended` event or 900 seconds after submission, whichever comes first.
3. Verify QA-B receives no QA-A event payload, run identifier, or private image reference. Verify QA-B's own data remains scoped to QA-B. Any disconnect before the capture boundary, missing QA-A terminal event, or missing positive QA-A event evidence makes the result `NOT-RUN`, not a pass. Do not reconnect and do not add another run for event capture.
4. Preserve read-only access through any already-authorized share link. Do not create, revoke, or modify a link. If no owner-supplied link fixture exists, record this control as `NOT-RUN`.

**Pass evidence:** both successful subscription handshakes; continuous owner/non-owner captures tied to the same run ID; QA-A's positive `session_started` and `session_ended` evidence; QA-B's bounded capture; and a separate record that the allowed share-link read was preserved or `NOT-RUN` because no authorized link fixture was supplied.

### REQ-003 — Image delivery in the current conversation

1. Use the pinned synthetic JPEG fixture. Stage it only through an action permitted by Sol's claim; verify its digest after staging.
2. In QA-A's current conversation, use `button[aria-label="Attach images"]` and `input[aria-label="Choose images to attach"]`. Verify the preview has alt text `Preview of screenshot.jpg`. Verify `button[aria-label="Remove screenshot.jpg"]` removes the selected image before reattaching it for the send.
3. Enter: `What date does the yellow instruction ask me to select? Reply with only the date.` Submit the sole QA-A run through `button[aria-label="Start task"]` only after both BUG-002 subscriptions are healthy and the provider gate is recorded.
4. Verify the same request contains the text and the pinned JPEG, the returned assistant answer is `11/07/2016`, and the answer and image belong to QA-A's current conversation. Confirm the matching event is visible to QA-A and absent from QA-B's private event/history views.

**Pass evidence:** fixture digest, same-request text and image evidence, current conversation/run ID, provider-call evidence with no credential values, returned answer, owner/non-owner event observations, and the served app revision.

## Negative controls and qualification

- Reuse the existing unsupported/malformed/oversize image regressions; they must reject before enqueue or provider activity. Do not duplicate them in a second source-level suite.
- The live non-owner phone-control contract is `UNKNOWN` and `NOT-RUN`. The safe pre-enqueue control is the read-only QA-B selector denial in BUG-001; it must stop before `/api/run`, enqueue, or device command. It does not prove server-side task-control authorization. The pinned source's `host_id` branch bypasses `require_device`; do not send a host-bound request or claim that the branch denies it. Sol must verify the serving revision and approve a separate reviewed control that proves rejection before enqueue and before any device command. Shared-device visibility and authorized share-link reads are preservation controls, not denials.
- If Sol determines this target class needs first-class qualification, run the Android squad's clean representative pass, clean rerun, and deliberate assertion-failure control against this same reviewed revision. For the control only, copy the journey and change only the expected answer assertion from `11/07/2016` to `11/07/2017`; keep the reviewed journey unchanged. The control goal is: `Open the same QA-A SmartQA conversation, attach the pinned screenshot.jpg, ask "What date does the yellow instruction ask me to select? Reply with only the date.", and assert that the returned assistant answer is exactly 11/07/2017.`

  Run the control only after both reviews, Sol's admission, QA-profile revalidation at the serving revision, and provider/account-owner/numeric-spend gates. Use a fresh session ID and the exact claim serial. Preserve the outcome artifact before evaluating it. The runner command must return `0`; the outcome must show `task_status="completed"`, exactly one failed assertion, and that failed assertion must name `11/07/2017`. The validator's expected exit is `1`; any other runner or validator result invalidates the control.

  ```bash
  CONTROL_GOAL='Open the same QA-A SmartQA conversation, attach the pinned screenshot.jpg, ask "What date does the yellow instruction ask me to select? Reply with only the date.", and assert that the returned assistant answer is exactly 11/07/2017.'
  CONTROL_SESSION_ID='<fresh session UUID>'
  CLAIM_SERIAL='<exact device serial in Sol claim>'
  CONTROL_TRACE_ROOT='<private control trace directory>'
  EVIDENCE_DIR='<authorized evidence destination>'
  set +e
  timeout --signal=TERM --kill-after=30s 900s env ARTEMIS_TRACES_DIR="$CONTROL_TRACE_ROOT" uv run artemis run "$CONTROL_GOAL" --profile pro --device-serial "$CLAIM_SERIAL" --standalone --session-id "$CONTROL_SESSION_ID" --enable-checker --verification-level final
  runner_status=$?
  set -e
  cp "$CONTROL_TRACE_ROOT/$CONTROL_SESSION_ID/run_outcome.json" "$EVIDENCE_DIR/qualification-control-run_outcome.json"
  test "$runner_status" -eq 0
  uv run python -c 'import json,sys; o=json.load(open(sys.argv[1])); t=o.get("tests",{}); f=t.get("failed_items",[]); ok=o.get("task_status")=="completed" and t.get("failed")==1 and len(f)==1 and "11/07/2017" in f[0].get("item_text","") and "11/07/2016" in f[0].get("evidence",""); print(json.dumps({"task_status":o.get("task_status"),"failed":t.get("failed"),"failed_items":f})); sys.exit(1 if ok else 2)' "$EVIDENCE_DIR/qualification-control-run_outcome.json"
  ```

  Keep the control `NOT-RUN` if any gate is missing. Include provider use inside the recorded numeric spend scope; Anthropic Android runs remain halted.
- Do not execute a control against a real phone or provider during authoring.

## Cleanup and evidence

- Preserve traces, screenshots, response records, and the exact testcase/runner/app revisions before cleanup. Redact credentials and personal data.
- Stop only run-owned work. Close only the test browser contexts. Remove the staged JPEG only if this run created that exact staged file. Remove only the disposable preview child created for the run.
- Do not clear personal app data, restart shared ADB, or alter shared-device, account, identity, route, Cloudflare, or share-link state.
- Sol verifies phone release. If cleanup is uncertain, keep the target unavailable and name the recovery owner.
- Return commands, exit status, host, serial, claim, app serving revision, run IDs, artifacts, first attempt/rerun/negative-control outcomes, and cleanup evidence to CHE-1143. Classify each unexecuted case as `NOT-RUN`, not `PASS`.
