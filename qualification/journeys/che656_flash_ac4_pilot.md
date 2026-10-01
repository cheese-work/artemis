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

Do not execute until two fresh independent non-author reviewers have accepted this exact testcase revision and Android records a separate exclusive device admission. Verify the configuration attachment's SHA-256 before use; a missing or mismatched file is a stop condition, not permission to fall back to the repository default. Use the named serial and the accepted private ADB endpoint only. A cold boot is expected after admission. Do not install an app, clear application data, or add a fixture; the target is the system Settings app. Run the exact goal once with the pinned profile, model configuration, serial, and standalone mode. Stop on the first result or error.

## Pass Criteria

1. The task result reports the battery percentage shown in Settings; do not assume a fixed percentage. Preserve the result and privacy-safe final-screen evidence.
2. Both `attempt_reconciliation_verdict.json` and `attempt_reconciliation_batch_verdict.json` exist in the returned trace and parse as JSON with `accepted: true`, `reason: null`, `detail: "1 attempt(s) accepted."`, and `invalid_attempt_count: 0`.
3. The retained attempt manifest and native `llm_usage` evidence identify the FlashRunner call as `anthropic:claude-sonnet-5`; independently reconcile its `FlashRunner` trace node to manifest node `operator` with node verdict `match`. A missing call or manifest is not a pass, even if the summary JSON says `accepted: true`.

The pinned runner serializes successful batch summaries with `reason: null`; it does not serialize the node-level `match` into either summary JSON. The pilot therefore requires both accepted summary files and the separate native usage/manifest evidence for `match`. Do not reinterpret `reason: null` as a mismatch or claim that the summaries themselves contain a `match` field.

Any missing/malformed output, unexpected model/provider, non-matching or unverified node identity, absent native usage, or task result that does not match the visible percentage is a failed or invalid attempt. A provider error is an infrastructure blocker, not a passing verdict; do not retry this testcase.

## Review Scope

Required independent OpenAI reviewers for this revision are `x99-codex-sol` (`5cd75ce5-d323-4004-9779-7e2ccf16bde4`) and `x99-gpt-6-astra` (`2e486c8c-88fb-4daf-b84a-9d26d75b33c7`). Each reviewer must inspect this same testcase digest and verify:

- The prompt, Flash profile, pinned v2 config, route evidence, runner SHA, device class, serial, and one-attempt boundary agree.
- The Settings observation can be checked without assuming a fixed battery percentage, and the cold-boot/no-fixture procedure is bounded to the later admitted target.
- Both summary files are required, and actual model identity plus `FlashRunner` → `operator` node-level `match` is proven from the attempt manifest and native usage, preventing an accepted-with-no-usage false pass.
- Missing evidence, config fallback, provider errors, retries, and any target or model drift fail closed and return to Terra without changing expectations.

Both reviewers must return scoped `agent_accepted` judgments on this exact digest before any device admission or execution. Any material testcase change requires both reviews again. This authoring revision contains no device, ADB, emulator, provider, fixture, or Artemis execution evidence.
