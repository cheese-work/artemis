# Session samples and automatic prompt tips

An empty Sessions chat offers two prompts copied from the upstream SDK examples:

- `artemis/sdk/examples/smart_notification_assistant.py`: the notification-panel task.
- `artemis/sdk/examples/video_transcription_example.py`: the first YouTube recording task.

Selecting a sample fills and focuses the existing composer. It does not start a run.

Managed task workers generate one automatic tip after the execution status becomes `completed`. The test outcome can pass or contain app defects. Failed, interrupted, cancelled and active executions make no automatic improvement-model call. Standalone SDK use does not trigger the admin-console feature.

The worker awaits generation before finishing teardown. The existing improvement service uses the server default model and a 90-second timeout. A transactional `automatic:<session_id>` claim prevents duplicate calls. Failed and abandoned claims do not retry automatically. A stale generating claim stops UI polling after the timeout grace.

The model receives bounded, redacted run evidence through the existing `prompt_improvement.build_input()` seam. Chat events and private-input requests are not prompt input. Generation errors log a static warning and the run ID, never the model error or request value.

The latest completed verdict includes the tip as a tertiary footer with a Copy button. The verdict remains the final timeline message. The UI polls while generation is pending and stops when generation settles. A failed generation leaves the completed verdict intact without a tip.

The Lessons navigation, route and stored-critique UI are removed. Existing immutable critique rows and backend APIs remain. Migration adds a nullable internal `automatic_state` column without replacing existing rows. Automatic tips reuse the same storage table.

Verification uses unit tests and synthetic browser fixtures. It is not Android device qualification.
