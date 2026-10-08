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
impossible to send. A missing `client_close` plus code `1006` is an abnormal
transport loss, not proof of a Cloudflare failure or a USB error. Page-unload
delivery is also best-effort. Background visibility is context, not a proven cause.

## Verification limits

Unit tests inject USB read/write failures and check server logs on both sides of
the loopback-attachment boundary. A hidden-tab clock test advances 142 seconds
without closing an attached relay. These tests do not reproduce physical laptop
sleep, actual browser throttling, or the original run `22d8d458`.

Real background-tab and laptop-sleep evidence still requires the Android device
squad's admission, independently reviewed testcases, and the dedicated physical
device/browser executor. No automatic reconnect is claimed: a new session currently
allocates a new loopback ADB serial rather than preserving the interrupted run's
transport identity.
