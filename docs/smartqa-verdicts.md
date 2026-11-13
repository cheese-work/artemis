# SmartQA verdict results

## Quickstart: labelled verdict locally in under 5 minutes

With the locked development dependencies installed, run the pure mapper without a device or model:

```bash
uv run python - <<'PY'
from apps.admin_console.services.qa_results import derive_result

result = derive_result(
    "failed",
    {"task_status": "failed", "cause": "goal_unreachable"},
    [],
)
print(f'{result["verdict"].upper()} · {result["reason"]} ({result["catalog_key"]})')
PY
```

Expected output:

```text
BLOCKED · Không đạt được mục tiêu (xem lại prompt) (blocked.user_prompt)
```

To exercise the chat endpoint with a fixture-backed run, run:

```bash
uv run pytest -q tests/unit/admin_console/test_chat_api.py::test_chat_adds_per_turn_results_without_changing_legacy_verdict
```

The shared builder is `tests/support/smartqa_verdict_fixtures.py`. It creates a local SQLite-backed chat, outcome, ledger rows, and evidence without a connected device.

## API shape

`GET /api/chats/{session_id}` keeps the existing `verdict` fields. It adds `verdict.result` for the latest turn and `results[]` for every turn. Each result includes:

- `catalog_key`, `verdict`, `verification`, `category`, and Vietnamese `reason`.
- `action.text` and an optional server-selected `action.href`.
- `responsible` and `evidence` (`turn_id`, `step`, `screenshot_url`).
- `turn_index`, `turn_count`, `label_state`, `source`, and `updated` (a version was appended).

The backend owns the label catalog. The API does not use agent-provided action URLs. Result reads keep `require_visible_run`; evidence URLs retain their own run-visibility checks. A caller who is not the owner does not receive the requester's email.

## Persistence and finalization

`qa_results` stores append-only versions keyed by `(turn_id, version)`. A known final category is immutable. An `unknown` result can be upgraded once by a later classification. When an unknown label remains pending beyond `ARTEMIS_VERDICT_PENDING_CAP_SECONDS` (30 seconds by default), the endpoint appends a final `unknown` row. Cancelled and legacy runs retain their explicit label states.

The result mapper is pure: `derive_result(execution_status, outcome, ledger_rows)`. A non-completed execution cannot pass. A known ledger category takes precedence over the agent's cause. The cause is used only when the ledger has no known category. App-defect results require a valid step citation and evidence; unverified citations become `unknown`.

## Activation and local maintenance

The first non-preview server startup records the activation timestamp and an immutable snapshot of `ARTEMIS_ADMIN_EMAILS` in `qa_result_activation`. Restarts and later admin-list changes do not move that window. Operators can record the snapshot explicitly before starting the server:

```bash
uv run python -m apps.admin_console.services.qa_result_commands activate --at '2026-10-20T10:00:00+07:00'
```

The sweep classifies terminal outbox events at or after activation. Earlier events still finish broadcast/notification delivery but do not trigger automatic historical classification. An explicitly replayed event bypasses that boundary. Classification errors retry up to 5 times, then mark the classifier abandoned without blocking the other consumers.

Backfill and replay are local CLI operations, not HTTP endpoints. They never stop or restart runs. Both accept a half-open time range `[since, until)` and `--limit` (default 100, maximum 1000). The default lower bound is 9 October 2026 at 00:00 ICT; the default upper bound is the command's start time. Explicit timestamps must include their timezone offset.

```bash
uv run python -m apps.admin_console.services.qa_result_commands backfill --until '2026-10-20T10:00:00+07:00' --limit 100
uv run python -m apps.admin_console.services.qa_result_commands replay --until '2026-10-20T10:00:00+07:00' --limit 100
```

Backfill uses terminal end times, includes old rows without an outbox entry, and skips turns that already have a result. Repeat a bounded command until `processed` is zero. Freeze `--until` when processing multiple batches. Backfill uses the same collector and result mapper as the live consumer. It does not alter the activation window.

Replay re-arms only rows whose classifier is abandoned. It clears the classifier's attempts, delivery mark and abandonment token, then lets the regular sweep retry. The durable `classify_replay_at` field records the explicit replay request. Broadcast/notification marks, attempts and abandonment tokens remain unchanged. Repeat until `replayed` is zero. An offline database needs a subsequent server sweep to execute the replay.

For an offline copy, place `--db PATH --traces PATH` before the subcommand. No device or model is required. Do not use maintenance commands to reset the activation snapshot.

## Admin relabel

`POST /api/system/qa-results/{turn_id}/relabel` requires the existing admin identity dependency. The request contains `category` and a non-empty audit `reason` (maximum 2000 characters). The server takes the actor from the verified identity. Unknown fields, including a caller-supplied actor, are rejected.

```json
{"category":"qa_environment","reason":"Confirmed that the device was locked"}
```

Relabel appends a final version and a `qa_result_audit` record in one transaction. The audit keeps the actor, timestamp, reason, previous/new versions and previous/new result JSON. SQLite triggers reject audit updates and deletes. Concurrent relabels serialize. An audit-write failure rolls back the result version.

The allowed categories are `smartqa_infra`, `smartqa_agent`, `provider`, `app_defect`, `user_prompt`, `qa_environment` and `unknown`. A category correction produces a Blocked result, except `app_defect`, which produces Fail and requires existing cited evidence. Pending and cancelled results cannot be relabelled. Unknown admin labels remain authoritative until another admin relabel. Automated classification never overwrites an admin label. The QA response sets `updated=true` after an appended version.

## Gate A

`GET /api/system/qa-results/gate-a` is admin-only. It returns 409 until activation exists. The fixed measurement window is the 7 days after activation, using each turn's start time. Follow-ups and admission failures without a `sessions` row count as separate turns. The snapshot admin list excludes admin-requested turns; unowned turns remain in the denominator conservatively.

- `N` / `denominator`: QA turns except `cancelled`, `running`, `queued` and `paused`.
- `numerator`: the latest final result has a verdict and a known category. Pass results with no failure category count. Unknown, legacy, pending and insufficient-evidence/check results do not count.
- `coverage`: numerator divided by denominator, or zero when N is zero.
- `cancelled`, `qa_turns`, `cancel_rate`: cancellations divided by all QA turns in the window, including live turns.
- `legacy` and `pending`: separate label counts; pending includes live turns and turns awaiting their first result. These are not extra exclusions from N.

`deploy_killed` counts structured `deploy_cut` reasons for all turns in the window, including admin turns. `passed` requires N greater than zero, at least 80% coverage, and zero deploy kills. The endpoint uses machine fields, never translated reason text. `window_complete` distinguishes a completed window from an interim measurement; an interim `passed` value is not deployment or acceptance authorization.

Run the maintenance regressions without a device:

```bash
uv run pytest -q tests/unit/admin_console/test_qa_result_operations.py tests/unit/admin_console/test_lifecycle_outcome_classification.py tests/unit/runtime/test_lifecycle_authority.py
```
