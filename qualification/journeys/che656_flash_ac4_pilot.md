# CHE-656 AC4 Flash Pilot

This is the immutable, one-attempt testcase for the CHE-656 AC4 Flash pilot. Its runner and configuration bindings are recorded in `qualification/candidates/che656_flash_ac4.v1.json`.

## Inputs

- Runner source: `cheese-work/artemis` commit `cbf2fea84cbd47ea54053239679ab521b01d4d23`.
- Configuration: CHE-656 attachment `01a0f55e-b007-725c-9253-4a07a907c5c3`, `che656-nova-pilot-artemis-v2.jsonc`, SHA-256 `2c06c1661b4f6cb65cadfa3b2141fa3f6ee7e0d5c399f43aee8bc481a7781274`.
- Offline route evidence: CHE-656 comment `01a0f566-9757-7dc7-8ce3-8dab64e6ff77`, verified 2026-10-01T02:58:56Z as `FLASH_ALL_CLAUDE_SONNET_5`.
- Goal: `Open Settings, find Battery and tell me the current level`.
- Profile/model: `flash` / `anthropic:claude-sonnet-5`.
- Target: class `x99_api35_medium_phone`, serial `emulator-5556`, private ADB port `5043`.
- Mode: `--standalone`; exactly one attempt, no retry.

## Preconditions And Procedure

Do not execute until two fresh independent non-author reviewers have accepted this exact testcase revision and Android records a separate exclusive device admission. Before any device boot or provider activity, verify the configuration attachment's SHA-256; a missing or mismatched file is a stop condition, not permission to fall back to the repository default. Set `ARTEMIS_ARTEMIS_JSONC` to that verified file and unset `ARTEMIS_FAKE_LLM`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `OPENAI_API_KEY`. Then run the loaded-settings gate below; it catches dotenv-loaded keys and the `GCP_API_KEY` alias as well as shell variables. Record only booleans, never key values.

```bash
set -euo pipefail
export ARTEMIS_ARTEMIS_JSONC="<verified-v2-config-path>"
test "$(sha256sum -- "$ARTEMIS_ARTEMIS_JSONC" | cut -d ' ' -f1)" = "2c06c1661b4f6cb65cadfa3b2141fa3f6ee7e0d5c399f43aee8bc481a7781274"
unset ARTEMIS_FAKE_LLM GOOGLE_API_KEY GEMINI_API_KEY OPENAI_API_KEY
uv run python -c 'import os; from artemis.config.settings import settings as s; assert not s.GOOGLE_API_KEY and not s.GEMINI_API_KEY and not s.GCP_API_KEY; assert os.environ.get("ARTEMIS_FAKE_LLM") != "1"; print("google_key_present=False gemini_key_present=False gcp_key_present=False fake_mode=False")'
multica attachment download 01a0f55e-b0df-72a5-a0be-36f0e3e3bca8 -o .
test "$(sha256sum -- route_evidence.py | cut -d ' ' -f1)" = "3d963ad481f894f9955b10a46f88b5c0d8b265836c2a0758875e1d91f62b922a"
uv run python route_evidence.py
```

The config hash must be `2c06c1661b4f6cb65cadfa3b2141fa3f6ee7e0d5c399f43aee8bc481a7781274`. The route script must match the digest in the receipt, run against pinned runner commit `cbf2fea84cbd47ea54053239679ab521b01d4d23`, and exit successfully with `FLASH_ALL_CLAUDE_SONNET_5`. Any failed check stops the attempt before boot/provider activity.

After admission, use only the named serial and accepted private ADB endpoint. A cold boot is expected. Within 300 seconds of the boot request, the exact serial must be online, `sys.boot_completed` must equal `1`, and privacy-safe evidence must show the UI is unlocked. If readiness times out, record a failed/invalid attempt and do not launch Artemis.

Immediately before the sole attempt, generate two fresh UUIDv4 values: one for `--session-id` and one for `--run-id`. They must be different and must not be reused from another run. Invoke the exact goal once with the pinned Flash profile, model configuration, serial, and standalone mode, supplying both flags. Record both IDs in the returned evidence; verify the session ID identifies the attempt manifest and native usage trace, and the distinct run ID scopes the batch verdict to exactly that one attempt, with no older batch members. Enforce a 900-second wall-clock deadline for the complete Artemis attempt with a supervisor that terminates only its recorded run-owned process group. A timeout is failed/invalid; do not retry. Do not install an app, clear application data, or add a fixture; the target is the system Settings app.

```bash
set -euo pipefail
SESSION_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
RUN_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
test "$SESSION_ID" != "$RUN_ID"
timeout --signal=TERM --kill-after=30s 900s uv run artemis run "Open Settings, find Battery and tell me the current level" \
  --profile flash --device-serial emulator-5556 --standalone \
  --session-id "$SESSION_ID" --run-id "$RUN_ID"
```

On every exit, preserve available evidence before cleanup. Stop only recorded run-owned Artemis, emulator, and private-ADB processes; restore any settings changed by the run; verify that the owned processes and private endpoint are released. Return cleanup evidence to Android Terra. If cleanup cannot be verified, leave the device unavailable with a named recovery owner.

## Pass Criteria

1. The completed task result reports the current-charge percentage shown on an identifiable `Settings > Battery` page. Same-attempt privacy-safe screenshot or hierarchy evidence must show the Battery page identity and its current-charge percentage; a status-bar percentage alone is not sufficient. The result must agree with that field. Missing page identity, unreadable or ambiguous evidence, disagreement, or an unsuccessful task result fails the oracle.
2. Both `attempt_reconciliation_verdict.json` and `attempt_reconciliation_batch_verdict.json` exist in the returned trace and parse as JSON with `accepted: true`, `reason: null`, `detail: "1 attempt(s) accepted."`, and `invalid_attempt_count: 0`.
3. The retained attempt manifest and native `llm_usage` evidence identify the FlashRunner call as `anthropic:claude-sonnet-5`; independently reconcile its `FlashRunner` trace node to manifest node `operator` with node verdict `match`. A missing call or manifest is not a pass, even if the summary JSON says `accepted: true`.

The pinned runner serializes successful batch summaries with `reason: null`; it does not serialize the node-level `match` into either summary JSON. The pilot therefore requires both accepted summary files and the separate native usage/manifest evidence for `match`. Do not reinterpret `reason: null` as a mismatch or claim that the summaries themselves contain a `match` field.

Any missing/malformed output, unexpected model/provider, non-matching or unverified node identity, absent native usage, or task result that does not match the visible percentage is a failed or invalid attempt. A provider error is an infrastructure blocker, not a passing verdict; do not retry this testcase.

## Review Scope

Required independent OpenAI reviewers for this revision are `x99-codex-sol` (`5cd75ce5-d323-4004-9779-7e2ccf16bde4`) and `x99-gpt-6-astra` (`2e486c8c-88fb-4daf-829e-e96be9c645b3`). Each reviewer must inspect this same testcase digest and verify:

- The prompt, Flash profile, pinned v2 config, route evidence, runner SHA, device class, serial, and one-attempt boundary agree.
- Fresh distinct session/run IDs guarantee both verdicts and bind the batch, manifest, and usage to one attempt; loaded-settings provider/fake-mode gates and the offline route preflight happen before boot.
- Boot/UI readiness and the whole attempt have explicit deadlines, timeout is fail-closed without retry, cleanup is verified before release, and the Battery-page/current-charge oracle cannot pass on status-bar evidence alone.
- Both summary files are required, and actual model identity plus `FlashRunner` → `operator` node-level `match` is proven from the attempt manifest and native usage, preventing an accepted-with-no-usage false pass.
- Missing evidence, config fallback, provider errors, retries, and any target or model drift fail closed and return to Terra without changing expectations.

Both reviewers must return scoped `agent_accepted` judgments on this exact digest before any device admission or execution. Any material testcase change requires both reviews again. This authoring revision contains no device, ADB, emulator, provider, fixture, or Artemis execution evidence.
