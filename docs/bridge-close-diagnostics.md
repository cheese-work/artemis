## Browser bridge close diagnostics

The relay sends a best-effort text frame before an intentional WebSocket close:

```json
{"type":"client_close","reason":"usb_write_error","usb_error":"NetworkError: transferOut failed","visibility_state":"visible"}
```

The server accepts this frame both before and after the loopback ADB connection.
It retains support for the old `close` frame. Each session's `event=bridge_close`
log includes `client_close`, `close_code`, and `close_reason` alongside the existing
session ID, serial, and server reason. Client diagnostics are allowlisted,
length-bounded, redacted, and JSON-escaped so they cannot inject log lines.

Client reasons distinguish manual disconnect, service destruction, page unload,
connection setup, socket errors, opening/attachment timeouts, bridge rejection,
invalid ADB data, USB read/write failures, and USB end-of-stream. USB write errors
are no longer reported as invalid server ADB packets. The existing interrupted-run
message is unchanged; reconnecting does not resume an interrupted run.

Browser warnings include the received WebSocket close code, reason, and clean-close
flag. The server records transport close codes/reasons from ASGI disconnect events.
`close_code=None` means no transport close frame was observed before session
cleanup, for example when a `client_close` report ended the relay first.

Unexpected network loss or laptop suspension can make a diagnostic frame
impossible to send. A missing `client_close` plus code `1005` or `1006` can indicate
transport loss; it is not proof of a Cloudflare failure or a USB error. Page-unload
delivery is also best-effort. Background visibility is context, not a proven cause.

## Verification limits

Unit tests inject USB read/write failures and check server logs on both sides of
the loopback-attachment boundary. A hidden-tab clock test advances 142 seconds
without closing an attached relay. These tests do not reproduce physical laptop
sleep, actual browser throttling, or the original run `22d8d458`.

Real background-tab and laptop-sleep evidence still requires the Android device
squad's admission, independently reviewed testcases, and the dedicated physical
device/browser executor. A transient socket failure can resume the existing session,
token and loopback ADB serial within the reconnect grace period. Opening a new
session allocates a new serial. This is separate from resuming an interrupted run.

## Recovery flag and endpoint readback (CHE-3077)

`ARTEMIS_BRIDGE_FIXES` defaults to off. After trimming whitespace and ignoring case,
only `on` enables it. With the flag on, an attached session without a client-close
report can resume after close code `1005` or `1006`. With the flag off, only `1006`
gets that treatment. Explicit socket-error/closed reports remain resumable in
either mode. USB disconnect, page unload, manual disconnect and service destruction
remain final. Both lease messages expose the server's flag value as `bridge_fixes`.

Read-only X99 service check, 02 Nov 2026, 15:20 ICT: `artemis-qa.service` was active
in `/home/congvc/services/artemis-qa`. `ARTEMIS_BRIDGE_FIXES` was unset in its launch
environment and `/home/congvc/.local/share/artemis-qa/.env`. Both configured inputs
therefore select off. This is a configuration readback, not an in-process flag
probe. No flag or service setting was changed.

The WebUSB bridge intentionally connects to `AdbEndpoint.local()`: a remote ADB
server cannot dial this server's loopback listener. Browser-run device enumeration,
readiness and queue binding also use that local endpoint, while other runs use the
global preference. A differing global preference is not itself a bridge failure.
CHE-1410 first fixed that routing in PR #136; its later PR #144 fixed duplicate
module imports that gave the queue a different, empty lease registry. Do not infer
an endpoint mismatch from the generic `device_offline` message alone.

The relay integration test sends ADB packets through a real loopback TCP stream
before and after two unreported `1005`/`1006` drops. It verifies the same session,
token and serial, bidirectional packet forwarding, one ADB connection and final
listener cleanup. A separate queue regression pins local routing with a remote
global preference. These are synthetic checks, not physical-device qualification.

Recommendation: enable `ARTEMIS_BRIDGE_FIXES=on` only through the separate
production deployment decision after review and passing checks. Read back the
flag from a lease during an admitted browser/device acceptance run. Validate both
close codes and deliberate-disconnect cleanup, and retain flag-off as the rollback.
