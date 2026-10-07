## Logging and privacy

The server and worker use the same handler-level `Redactor`. Artemis console,
DataEngine, file, provider-retry, root and uvicorn handlers redact configured
credential values, recognizable secret patterns and the current task's goal.
Context-local session IDs keep concurrent worker logs separate. Worker output
is decoded and redacted by complete line before console/file forwarding.
Lines over 64 KiB are replaced rather than partially forwarded.

Goals reach workers through single-use, mode-0600 files, not process arguments.
The worker deletes the file after reading; the parent cleans up after spawn
failure, cancellation or process exit. Task logs and notifications include only
goal length and a short SHA-256 identifier. Tool logs include only argument length.
Redaction cannot be disabled.

The model still receives the goal. Non-log trace payloads, screenshots and video
can contain user data; logging redaction is not media or whole-trace sanitization.
Download helpers keep their existing `redact_text` / `redact_json` interfaces.
Python consumers can also use `artemis.utils.redaction.redact(value)`.

The UI uses `LoggerService`. Set `localStorage['artemis.log']` to `debug`, `info`,
`warn` or `error` in the browser console; the default is `warn`. Errors remain
visible at every supported level. The exported `redact()` helper also protects
Copy run summary. Browser-storage, stream and cleanup failures are reported.

## Worker/journal acceptance probe (X99)

Run from the repository with an installed virtual environment:

```bash
PYTHONPATH=. .venv/bin/python scripts/check_log_redaction.py ./log-redaction-evidence
```

The probe launches the real worker CLI through `systemd-cat`. Diagnostic fault
injection exercises root logs, Artemis-owned handlers, a configured credential,
an exception and split stdout writes with a synthetic password in the goal.
The real CLI then rejects an intentionally invalid option before device/model
execution. The probe reads journald without modifying journal state. PASS requires
all control events, zero goal/password/credential occurrences, no password in
argv, and deletion of the private goal file. It records the validation failure
explicitly; it is not Android E2E or a successful model run.

## Lightweight usage (no metrics service or pilot gate)

Use the existing service journal and key-value events. Counts are operational
signals, not unique-user analytics; restarts/retries affect the totals.

```bash
journalctl -u artemis-qa.service --since today --no-pager -o cat | rg -c 'event=task_started'
journalctl -u artemis-qa.service --since today --no-pager -o cat | rg -c 'event=task_finished .*status=completed'
journalctl -u artemis-qa.service --since today --no-pager -o cat | rg 'event=(bridge_|recorder_|model_call_)'
```

Bridge events use CHE-1048's `bridge_lease_created`, `bridge_adb_connect`,
`bridge_adb_disconnect` and `bridge_close` names and `session_id` field.
