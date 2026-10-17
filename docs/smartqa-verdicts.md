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
- `turn_index`, `turn_count`, `label_state`, and `source`.

The backend owns the label catalog. The API does not use agent-provided action URLs. Result reads keep `require_visible_run`; evidence URLs retain their own run-visibility checks. A caller who is not the owner does not receive the requester's email.

## Persistence and finalization

`qa_results` stores append-only versions keyed by `(turn_id, version)`. A known final category is immutable. An `unknown` result can be upgraded once by a later classification. When an unknown label remains pending beyond `ARTEMIS_VERDICT_PENDING_CAP_SECONDS` (30 seconds by default), the endpoint appends a final `unknown` row. Cancelled and legacy runs retain their explicit label states.

The result mapper is pure: `derive_result(execution_status, outcome, ledger_rows)`. A non-completed execution cannot pass. A known ledger category takes precedence over the agent's cause. The cause is used only when the ledger has no known category. App-defect results require a valid step citation and evidence; unverified citations become `unknown`.
