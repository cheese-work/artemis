# CHE-2927 Mobile Chat Steer And Supplement States

This is the reviewed testcase candidate for native verification of mobile PR #767. It is instructions and assertions only. It contains no device execution or acceptance evidence.

## Pinned inputs

- Mobile app source: `cheese-work/multica` commit `c6812181ab042ec395e62ea14d98cc7f1f5f26e3` (PR #767).
- Server contract: `cheese-work/multica` PR #763 commit `272a5996013d65be5014ab8172da559f84a06bac`.
- Artemis runner: `cheese-work/artemis` commit `feefd94c72dbce0fa1afc109ffe3d8254f565d6b`, the general qualification runner pinned by the maintained qualification manifest. Do not use PR #201's `--qualification che984-d1` mode; that mode is restricted to its synthetic package.
- Candidate receipt: `qualification/candidates/che2927_mobile_chat_steer.v1.json`.
- Expected package: `ai.multica.mobile`; verify the built APK's package metadata before install. Any mismatch is a stop condition.

The app SHA is pinned, but an APK digest is intentionally absent until the app is built on X99. Do not substitute an APK from another source revision.

## Preconditions

Execution starts only after two independent non-author reviewers accept the same receipt and journey revision, and Sol records an issue-visible exclusive claim for the exact X99 AVD serial. No install, launch, reset, ADB, emulator, fixture or Artemis command is authorized by this document before those conditions.

The execution owner must have all of these inputs ready before claiming the device:

1. An X99-built APK from the pinned mobile SHA, with its SHA-256 and package metadata recorded.
2. The local server at the pinned PR #763 SHA, reachable from the AVD, with a test workspace and authenticated DM session.
3. Deterministic seed/reset fixtures for one running head task, four distinct queued follow-ups, the `capable`, `starting` and `unsupported` head-supplement values, and message receipts in `pending`, `delivered`, `failed` and `uncertain` states. The uncertain and failed receipts must be server-seeded; never simulate them in the app.
4. A stub head agent that stays running without replying for the queue and Steer scenarios.

If any input is missing, record it as NOT-RUN with its named owner/input. Do not assert or screenshot a state that the server did not return.

## Fixture and repeatability

Use a unique run suffix in every seeded message body so the selected task and bubble are unambiguous. Before each scenario, reset only this test workspace/session to its documented baseline, seed the requested state, and verify the server response contains the expected task IDs, head status and receipt status. Keep the same authenticated test session; do not read personal data.

Use the OS appearance setting to select light and dark mode. Record the starting setting, verify the app reflects each selected mode, and restore the original setting during cleanup. Capture screenshots only after the target state is visible and settled. Keep screenshots privacy-safe and limited to the seeded test DM.

After the scenario set, reset the seeded server fixture, stop only the process owned by this testcase, restore the original appearance setting, and verify the test session and run-owned processes are released. Record cleanup on success and failure. If cleanup cannot be verified, leave the AVD unavailable and name a recovery owner.

## Scenarios and assertions

Run each scenario once in light mode and once in dark mode. Capture one screenshot per scenario per mode, plus the second queue screenshot after scrolling: 18 screenshots total. Include the mode, scenario ID and exact testcase revision in each artifact name.

1. **Long queue with keyboard open (`long_queue_keyboard`).** Seed a running `capable` head and at least four distinct queued follow-ups. Open the DM, focus the composer, and leave the keyboard visible. Locate the queue by `mobile-chat-queued-follow-ups` and the composer by `mobile-chat-composer`. Assert the composer remains visible above the keyboard, no queue content covers it, no more than three follow-up rows are visible at once, and scrolling the queue reveals the fourth uniquely seeded row while the composer stays visible. Capture the visible first rows and a second screenshot after scrolling to the fourth row in each theme.
2. **Steer a capable head (`steer_capable_head`).** Seed a running `capable` head and a uniquely identifiable queued follow-up. Locate its row inside `mobile-chat-queued-follow-ups`, activate its accessible `Steer` button, and wait for the pinned server receipt/refetch. Assert that task leaves the queue and its message bubble displays `Added to run`. A tap alone, an optimistic update, or an HTTP request without the server-backed UI state is not a pass.
3. **Starting fallback (`fallback_starting`).** Seed a running head whose `head_supplement` is `starting`, plus one queued follow-up. Assert visible reason `Run is starting`, and assert the row's accessible `Steer` button is disabled. Verify the queued item remains and no steer request is recorded.
4. **Unsupported fallback (`fallback_unsupported`).** Seed a running head whose `head_supplement` is `unsupported`, plus one queued follow-up. Assert visible reason `This run can't take new messages. The message stays queued.`, and assert the row's accessible `Steer` button is disabled. Verify the queued item remains and no steer request is recorded.
5. **Added receipt (`supplement_added`).** Seed a bubble with a `pending` receipt (the `delivering` state has the same presentation). Assert `Added to run` is visible and no resend action is offered.
6. **Read receipt (`supplement_read`).** Seed a bubble with a `delivered` receipt and no post-resend acknowledgement. Assert `Read by agent` is visible and no resend action is offered.
7. **Not received receipt (`supplement_not_received`).** Seed a bubble with a `failed` receipt and no prior resend. Assert `Not received` and `Send as a new run` are visible. Activate the action once; assert the request uses `confirmPossibleDuplicate: false`, a new run is created, and the updated bubble indicates it was sent again.
8. **Uncertain receipt and duplicate confirmation (`supplement_uncertain_confirm`).** Seed a bubble with an `uncertain` receipt and no prior resend. Assert the label is not `Not received` and `Send again anyway` is available. Activate it and assert the native confirmation alert shows the duplicate-risk title/body with `Cancel` and `Send again anyway`. Cancel first and verify no resend request was sent. Reopen the dialog, confirm, and verify the server receives `confirmPossibleDuplicate: true` exactly once and returns a resent receipt. Capture the open confirmation dialog in both themes.

Use exact English text above by setting the app locale to English in the test fixture. For accessible buttons, assert both the accessible role/name and disabled state where specified. If the runner cannot expose a native alert or stable row target, stop and report the limitation; do not replace it with coordinate-only evidence.

## Evidence and verdict

For every scenario, return the Artemis trace, exact command and exit status, device class and serial, app SHA and APK digest, server SHA, runner SHA, fixture seed/reset identifiers, screenshots, observed assertions, and cleanup/release evidence. Keep test messages synthetic. Record failures as assertion failures, infrastructure failures or unavailable-device outcomes; missing prerequisites or missing artifacts are NOT-RUN/BLOCKED, never PASS.

The queue screenshot must visibly show the keyboard, composer and queue together. The fallback screenshots must visibly show the reason and disabled `Steer` action. Receipt screenshots must show the relevant bubble label and action. The uncertain screenshot must include the native confirmation dialog. Pair each artifact with its light/dark mode and scenario ID.

## Review boundary

Two independent non-author reviewers inspect this exact receipt and journey revision before any device execution. Reviewers check selectors, server-seeded states, repeatable setup/reset, assertion observability, false-pass guards, screenshot coverage and cleanup. Any material change requires both reviews again. The author does not approve this testcase. This document does not claim APK readiness, fixture readiness, an Artemis run, a device qualification, or a pass.
