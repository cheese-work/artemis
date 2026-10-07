# Browser bridge close reproduction packet

**Status:** Provisional author packet. This is not execution evidence or testcase approval.

**Source:** PR 85 head `bcaff36bf0e439ffba22b837441514ea9c0beb2c` and `docs/bridge-close-diagnostics.md`.

No phone, browser tab, or laptop sleep control was used to prepare this packet. Do not start a physical run until Sol re-evaluates admission, both designated reviewers approve this exact packet revision, and the MBP executor has an issue-visible grant. The laptop-sleep case is `NOT-RUN` unless Sol's grant explicitly permits sleep control.

## Common setup

Use the same pinned Artemis runner, testcase, app build, browser build, and admitted Pixel 6 Pro for all three legs. Record each exact revision, the assigned serial and transport, run ID, browser version, session ID, loopback serial, and UTC start time. Use a fresh bridge session for each leg; a new session can allocate a new loopback serial, and it does not resume an interrupted run.

Choose an already-reviewed bridge-backed testcase that remains active for at least 142 seconds. The interval matches the reported 2 minutes 22 seconds. Do not change its steps or expected results to force the duration. If no reviewed testcase meets this condition, mark the run `NOT-RUN` and report the missing prerequisite.

For each leg, capture the browser console and server log lines for the session. Do not close the tab, reload the page, click Disconnect, or inject a USB fault during an exposure interval.

## Test legs

1. **Negative control — foreground and awake:** Start a fresh bridge-backed run. Keep the owning tab visible and the laptop awake for 142 seconds. The bridge must remain connected and the run must continue. A `bridge_close` event during this interval invalidates the control; stop and report it before running the exposure cases.
2. **Background tab:** Start a fresh bridge-backed run and confirm normal activity while its tab is visible. Switch to another tab for 142 seconds without closing or reloading the bridge tab. Restore the bridge tab. Record whether the session stayed connected, whether the run completed or was interrupted, and all matching browser and server logs.
3. **Laptop sleep:** Start a fresh bridge-backed run and confirm normal activity. Use the laptop's ordinary sleep action for 142 seconds only if the grant permits it. Wake the laptop and restore the bridge tab. Record the same outcomes and logs as the background-tab case. Do not automate or issue sleep controls from the test runner.

## Expected observations

The browser console can contain:

```text
Device bridge socket closed: { code: <number>, reason: <string>, was_clean: <boolean>, visibility_state: <visible|hidden> }
Device bridge closing: { type: client_close, reason: <client_reason>, usb_error: <string|null>, visibility_state: <visible|hidden> }
```

The server close record has this shape:

```text
event=bridge_close session_id=<id> serial=<loopback-serial> reason=<server-reason> close_code=<number|null> close_reason=<JSON-string> client_close=<JSON-object|null>
```

When the client report reaches the server, `reason=client` and `client_close` contains `reason`, `usb_error`, and `visibility_state`. A browser close can happen before the best-effort report arrives. In that case, record the actual server reason, `close_code`, and `close_reason`; `client_close=null` is valid evidence. `close_code=1006` indicates abnormal transport loss. It does not prove Cloudflare failure, browser throttling, or a USB error.

Count an exposure leg as reproducing the symptom only when the bridge closes during its 142-second condition and the run records the interruption. Record whether the existing “Run interrupted ... The phone went offline” message remains visible. A leg with no close is `NOT-REPRODUCED`, not proof that its cause is absent. Do not expect automatic reconnect or continuation of the interrupted run.

## Cleanup and evidence

After each leg, save the run outcome and matching browser/server logs before cleanup. Stop only the run owned by that executor. If the bridge remains open, disconnect it through its normal UI. Restore the tab to the foreground and wake the laptop. Record `event=bridge_adb_disconnect ... result=disconnected` when emitted. Do not reset shared ADB or claim another serial. Sol verifies device cleanup and release; if cleanup is uncertain, leave the target unavailable and report the recovery owner.
