# Private device input

During a chat turn, the agent focuses an editable field and saves an `input_request` note with JSON containing `label` and `secret` (default `true`). The next step boundary replaces the QA composer with a password input. Secret requests require an Android field marked as a password field. Unsupported drivers fail closed.

The authenticated owner or administrator submits the value through the session-bound input endpoint. Other QAs viewing a shared session cannot submit. The value travels through a per-request Unix socket to the worker and then directly to the private device action. It never enters ordinary chat messages, model history, persisted events or action results. The request accepts one value, expires after 180 seconds, and rejects a changed focused target. The timeline stores metadata and a masked receipt only.

Sockets use mode `0600` in an owner-only `0700` directory. The API and worker must share that directory and OS user. `ARTEMIS_INPUT_SOCKET_DIR` overrides the default `/tmp/artemis-input-<uid>`. Platforms without Unix sockets return an unavailable error rather than a less-private fallback.

Before requesting a secret, the worker stops recording. The existing recording prefix remains available. Recording cannot restart during the private-input turn, and the raw live stream suppresses that device's frames for the remainder of the active turn. Password text, XML attributes and screenshot regions are masked at the shared observation boundary. Malformed hierarchy data fails closed. These protections do not promise safety against a malicious application that deliberately echoes credentials elsewhere.

The private Android action bypasses the clipboard and ordinary action/trace executor. It revalidates focus and selectors. ADBKeyboard supports UTF-8 input; native input rejects unsupported Unicode and literal `%s` rather than changing a credential silently. Device errors produce generic results, never echoed values.

Verification uses fake device commands, single-use/replay/expiry cases, socket permissions and cleanup, a sentinel across database/log/trace/model consumers, and a deliberately failing leak detector control. These checks are not live-device qualification.

Unknown stream-device identity fails closed whenever any private-input request is active. XML nodes sharing password bounds receive the same masking, including ancillary accessibility text. A static warning makes malformed-hierarchy blackouts observable without logging the hierarchy. Shell exit status and broadcast completion are checked; the receipt confirms device-command delivery, not application acceptance. A 60-second stream privacy grace covers just-finished or cancelled in-flight delivery. Cancellation never asserts that an already-started action sent no input.
