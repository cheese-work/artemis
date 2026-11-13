## LLM providers

Server admins (`ARTEMIS_ADMIN_EMAILS`) open **LLM providers** from the sidebar
or account menu. All provider-settings endpoints require server-side admin
authorization. A QA receives 403, including reads, Test, and Sync.

There are five fixed slots: OpenAI (Chat Completions), Google (Gemini API),
Anthropic (Messages), xAI, and OpenRouter. Official and sub2api (C00) presets
fill the URL; Custom retains an editable URL. Native Google and Anthropic
URLs are roots. Chat Completions URLs include `/v1` (OpenRouter `/api/v1`).
HTTPS is required except for localhost and tailnet HTTP endpoints. Embedded
credentials, query strings, and fragments are rejected. Redirects are not
followed; provider requests time out after 10 seconds, with a 30-second action
deadline and bounded model-list pagination.

Save changes before Test or Sync. Blank keys preserve the existing key.
To remove a key, disable the slot and select **Clear saved key**. Only a
masked suffix is returned; keys are never stored in browser storage. Errors
redact credentials before display or persistence. Key-change audit records
contain only the event, authenticated actor, and timestamp. Provider actions
do not create run traces. Loaded and replaced keys are registered with the
shared logging/trace redactor.

Test lists models and sends one minimal protocol-native inference request.
It records latency, success, or a sanitized provider error. Sync lists models
and persists the results without inference. A failed same-configuration Sync
retains the last successful list and reports its error. A key or URL change
clears models and previous results. Concurrent saves use an optimistic version;
in-flight results are refused if the slot changes.

The private `artemis-providers.json` file sits beside the canonical `.env`.
It is written atomically with mode 0600 under the existing configuration lock.
It is ignored by Git. Startup imports environment keys and URLs, including the
operator's custom endpoint. Each unsaved slot continues to follow `.env` and
service environment precedence, with the existing 600-second discovery cache.
After a slot is saved, its configuration becomes authoritative. Clear and
disable write empty worker environment variables for every key alias, so
worker dotenv loading cannot resurrect an old credential. Saved URLs override
stale JSONC endpoint overrides in private worker snapshots. Original `.env`
values are not deleted. Existing runs keep their launch snapshot.
Saved slot credentials also update server readiness after Save and restart.
The older Setup page shows saved slots as read-only and refuses credential
or URL changes for those slots. Use LLM providers instead. Saving or syncing
invalidates the browser's model-catalog cache. Workspace remains available
when another enabled slot has synced models but the default lacks a key.

`GET /api/models` reads enabled slots' synced lists. Saved slots do not scan
keys automatically: use Sync to refresh them. The configured deny list still
applies. Both explicit and default run models must be in the enabled catalog;
otherwise admission returns `model_not_available` before device probes or
queue writes. Per-run explicit choices retain same-model retries and no
fallback. The picker labels models by protocol slot, not inferred model vendor.

Gemini transported through Chat Completions remains in OpenAI. Google needs a
Gemini-native endpoint or direct Google key; C00 endpoint provisioning is a
separate operation. The sub2api preset does not promise native support.
Production-provider management is disabled in isolated PR previews.

## API

- `GET /api/system/providers`: version and masked slot status.
- `PUT /api/system/providers/{provider}`: version, base_url, enabled, optional write-only key.
- `POST /api/system/providers/{provider}/test`: native list and inference probe.
- `POST /api/system/providers/{provider}/sync`: refresh persisted models.

These routes return the updated masked snapshot. Validation errors do not echo
submitted values. Status timestamps are stored as Unix seconds and displayed
in ICT (UTC+7).
