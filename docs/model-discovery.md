## Per-run model selection

The authenticated `GET /api/models` endpoint scans the server's configured keys.
It returns `models` rows (`provider`, `model`, `source`), the effective operator
`default`, and sanitized per-provider `errors`. It never returns credentials.
OpenAI-compatible proxies keep the `openai` provider even for non-OpenAI model IDs.

OpenAI, OpenRouter and xAI use their `/models` endpoint. Google lists models
that support `generateContent`; Google and Anthropic pagination is followed.
Native Google and Anthropic base URLs are roots, matching their SDKs; discovery
appends `/v1beta/models` and `/v1/models`, respectively.
The scan uses the same current `.env` and service environment precedence as
worker launch. Provider base URL overrides and the operator's `api_base` apply.

Results are cached in memory for 600 seconds. A failed refresh retains the
last good list for the same key and endpoint and reports an error. Removing or
changing a key discards its old list. New models on an existing proxy key appear
after the next refresh without a code change.

Configure exact denied IDs or `provider/model` pairs in `artemis.jsonc`:

```json
"model_discovery": {"deny": ["codex-auto-review", "gpt-reserve"]}
```

These two IDs are denied when the setting is absent. An explicit list replaces
the defaults, and deny changes apply to cached rows immediately.

`POST /api/run` accepts an optional `model: {provider, model}`. Explicit choices
must be in the current discovered list or receive `400 model_not_available`.
Omitting the field uses the configured operator default, even during a scan
outage. The selected pair is bound to the queue item and recorded with the run.
Only the worker's private operator configuration changes; the server default
and other runs are unchanged. The execution snapshot records the operator model.

Every QA can freely choose any discovered model. There is no per-user or
per-project selection policy or quota cap. Explicit selection disables the
operator fallback chain, including when the selected pair equals the default.
Existing retries still call the same model. Exhausted quota, 429, unavailable
models and exhausted provider errors fail the run instead of pausing or switching
models. The failure slot names the selected model and cause and asks the QA to
choose another model. Omitting the choice preserves the default retry, fallback
and pause behavior unchanged.

The composer lists the default first. Choices are remembered separately for
each signed-in user, with preview storage isolation. Missing or removed choices
fall back to the default. The run header shows the recorded model ID, not the
Flash/Pro architecture label. The native select supports keyboard operation and
has a minimum 44 px touch target.

PR previews use a synthetic model list and never scan production credentials.
Focusing the picker refreshes the list after its 600-second client cache expires.
