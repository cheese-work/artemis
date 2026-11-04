# Prompt improvement and lessons

A finished run's **Improve prompt** action generates a ready-to-run prompt only
when clicked. It uses the server default model, independently of the run's
execution model, without a fallback. Each click is one structured generation
with at most one corrective re-ask for malformed output and a 90-second timeout.
It never pauses other runs. Users can edit the result and start a new run with
any execution model; the Workspace consumes that auto-start handoff once.

The pure `build_input` function collects the original goal, step-stamped user
steers, failed actions/results, recent steps and the run verdict. Interrupted
runs or three consecutive failed steps are marked stuck. Failed steps precede
the last ten steps. Summaries are capped at 400 characters, and total input at
32,000 characters; omitted evidence is counted. Input and stored output are
redacted. Evidence is treated as untrusted data in the model instruction.

| Route | Access and response |
| --- | --- |
| `POST /api/sessions/{session_id}/improve-prompt` | QA owner or admin; returns a newly saved critique. Active runs and concurrent generation for the same session return 409. Preview disables this route. |
| `GET /api/sessions/{session_id}/prompt-critique` | Evidence share-by-link access; newest saved critique, or 404. Never generates. |
| `GET /api/prompt-lessons?q=&scope=&limit=&offset=` | Lists visible critiques newest first; scopes `mine`, `available`, or admin `all`. Searches original/improved prompt, changes and lessons. Returns `critiques` and `next_offset`. |

Model output is validated as `improved_prompt` (nonempty string), `changes`
(at least one `{what, why}` pair), and `lessons` (one to five nonempty strings,
each at most 400 characters). Model failures return 502 `improve_model_failed`
with a redacted model name and cause. Unparseable/invalid output returns 502
`improve_parse_failed` and saves nothing.

The additive SQLite module `prompt_critiques` uses `schema_revisions.apply`
during storage initialization. Its table stores `id`, `session_id`,
`created_at`, `requested_by`, `model`, `original_prompt`, `improved_prompt`,
`changes_json` and `lessons_json`. Every successful click adds a row. Removed
runs are hidden immediately; physical purge deletes their critiques, and
migration cleans up orphan rows left by older binaries.
