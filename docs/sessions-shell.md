## Sessions shell (UX v2, S1)

`/sessions` lists chat sessions. `/sessions/{id}` opens a session without changing
the phone selected for a new run. `/sessions/new` opens an empty composer and
retains the first-run setup guard. `/runs` and `/runs/{id}` redirect to Sessions;
`/workspace` redirects to the new-session composer.

Run evidence IDs remain unchanged. Catalog rows include `chat_session_id`,
`chat_title` and `chat_prompt` when chat metadata exists. The browser groups
cursor pages by chat session, retaining the newest turn's status and the first
prompt. Legacy runs remain individual sessions.

`PATCH /api/chats/{id}/title` accepts a trimmed, nonempty title of at most 120
characters. Any authenticated QA who can view the full session ID can rename
the session, including through a turn ID. This does not grant message, pause,
continue or device-control permissions. SQLite retains the override in
`chat_sessions.title`; shared catalog and chat responses retain redaction.

Navigation has pinned expanded, pinned icons and expand-on-hover modes. The
browser saves the choice separately for each user and application base path.
The right sidebar has Live, Details and Stats tabs. Pause and Stop are in the
composer. Cancel remains available for queued runs.
Narrow layouts stack the sidebar below the chat instead of covering the composer.

Live uses the existing device stream only when its reported phone matches the
session binding. The stream checks the requested serial on every frame, even
for administrators. It never substitutes another connected phone. Unsupported
or unavailable live transport reports an unavailable feed; recorded evidence
and the timeline remain available. Completed runs show the recording when
available. Details timestamps include ICT.

S2 replaces the existing turn evidence disclosures with typed, flat timeline
events. S3 adds ephemeral device input. S4 adds upstream examples and inline
improvement tips and removes Lessons. These are not part of S1.
