# Shared run list

`RunLibraryComponent` is the full-width Runs page and the compact Workspace history panel. Both render `RunCardComponent`. The live Workspace queue remains owner-scoped and separate.

My runs is the default. `scope=everyone` selects Everyone's runs. Search, status, dates, extra filters, and scroll position remain URL state. Arrow keys, Home, and End operate the owner tabs. Changing tabs resets the page cursor and clears stale rows.

Only the catalog's `/api/runs` endpoint accepts `scope=everyone`. In authenticated mode it requires a verified identity, excludes unowned runs for non-admins, redacts prompts, and returns owner emails and `read_only`. `scope=all` remains admin-only; queue and stream permissions do not change. Direct run lookup marks non-actionable runs read-only and redacts their prompts.

In authenticated Everyone's runs, non-empty text search matches only the caller's own runs, including for administrators. This restriction covers FTS and substring fallback so another owner's raw prompt cannot be inferred through search results. Blank searches and other filters still list shared runs. Open mode remains unfiltered. Ambiguous-id candidates use the same presentation and redaction as direct lookup.

Workspace history reloads when the terminal-session revision changes, without changing scope or filters. Unchanged polling snapshots do not reload the catalog. Compact history scrolls inside the shared component, bounded to 60vh, so saved and restored positions refer to the actual scroller.

Team links open the viewer in read-only review mode, including for administrators. Copy link and Download remain available; pin and delete are absent and their handlers are guarded. Stop and Resume are not viewer actions. URL state is not an authorization boundary: existing server ownership checks still protect mutations.

Videos and screenshots are not redacted. The team tab and existing media viewer keep that warning. Compact history preserves recorded device labels without selecting a live session or changing the next run's device.
