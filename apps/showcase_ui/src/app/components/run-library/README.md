# Shared run list

The list opens U2's `RunViewComponent`; team-route and server read-only state guard pin, delete and resume in the unified controller. Compact history retains recorded goal-image thumbnails and uses `mediaUrl` for preview-scoped image paths. Its My runs scope is explicit and does not inherit the administrator's live-queue All users setting.

`RunLibraryComponent` is the full-width Runs page and the compact Workspace history panel. Both render `RunCardComponent`. The live Workspace queue remains owner-scoped and separate.

My runs is the default. `scope=everyone` selects Everyone's runs. Search, status, dates, extra filters, and scroll position remain URL state. Arrow keys, Home, and End operate the owner tabs. Changing tabs resets the page cursor and clears stale rows.

Only the catalog's `/api/runs` endpoint accepts `scope=everyone`. In authenticated mode it requires a verified identity, excludes unowned runs for non-admins, redacts prompts, and returns owner emails and `read_only`. `scope=all` remains admin-only; queue and stream permissions do not change. Direct run lookup marks non-actionable runs read-only and redacts their prompts.

In authenticated Everyone's runs, non-empty text search matches only the caller's own runs, including for administrators. This restriction covers FTS and substring fallback so another owner's raw prompt cannot be inferred through search results. Blank searches and other filters still list shared runs. Open mode remains unfiltered. Ambiguous-id candidates use the same presentation and redaction as direct lookup.

Workspace history reloads when the terminal-session revision changes, without changing scope or filters. Unchanged polling snapshots do not reload the catalog. Compact history scrolls inside the shared component, bounded to 60vh, so saved and restored positions refer to the actual scroller.

Team links open the viewer in read-only review mode, including for administrators. Copy link and Download remain available; pin and delete are absent and their handlers are guarded. Stop and Resume are not viewer actions. URL state is not an authorization boundary: existing server ownership checks still protect mutations.

Videos and screenshots are not redacted. The team tab and existing media viewer keep that warning. Compact history preserves recorded device labels without selecting a live session or changing the next run's device.

Prompt attachments are not shared screenshots. In authenticated mode, goal-image bytes require the run's owner or an administrator, including through generic media URLs after path resolution. Open mode remains unchanged. Bundles exclude goal attachments, even if a recording reference points to the attachment folder; exported text is always redacted.

This attachment rule also covers inline images recorded in LLM traces. Shared session JSON removes inline image blocks, base64 image data and image references before the trace viewer can materialize them. Trace downloads use the recorded session's owner, not a caller-supplied session id. Owners and administrators keep their trace view; their decoded images are cached inside the run's `goal_images` folder and fetched through owner-checked opaque `/images` references. Open mode keeps the existing public image cache behavior.

Historical hash-named files in `traces/images` without an `images` capture record have no recoverable owner. Authenticated QAs cannot fetch those legacy inline caches through `/images`, `/api/images` or `/local_file`; administrators can. Recorded screenshots remain shared. Bundles omit legacy inline cache bytes and strip inline image data and references from all exported text, including notes, logs and checks. No historical files or capture records are deleted or migrated.
