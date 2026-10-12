## SmartQA chat sessions

Frontend runs are chat sessions. Each turn retains its own existing run ID for
steps, recordings, verdicts and defect scanning. The additive `chat_sessions`,
`chat_turns` and `chat_messages` tables backfill old runs as single-turn sessions.
Existing evidence IDs do not change. CLI, SDK and MCP one-shot calls are unchanged.

`GET /api/chats/{run-id}` resolves a turn to its session. Send text with
`POST /api/chats/{session-id}/messages` and a stable `client_id`. Retries reuse
that ID; reusing it for different text returns a conflict. Pause and Continue
use separate session-scoped endpoints. All writes require the session owner
or an administrator. Shared history is read-only and redacted.

SQLite sequences order messages. The worker acknowledges opening bubbles in
order. Both Flash and Pro poll before the next agent step. Mid-run messages use
the run's Operator model to judge a plan change. A correction steers by default.
A judged change, or an invalid/unavailable judgement, pauses and adds an agent
reply. Continue or another human message resumes the loop. A crash before the
acknowledgement leaves the message pending. Acknowledgement and reply commit
together. Unread messages at the final-step boundary become the next turn.

The frontend worker holds its existing exclusive device lease between turns.
`ARTEMIS_CHAT_IDLE_SECONDS` defaults to 300. After idle expiry, a follow-up
re-enters the normal queue with its original device and model binding. A missing
or changed endpoint fails clearly; the worker never silently selects another
phone. Legacy runs without a saved launch binding can be read but cannot be
resumed on an unverified phone. Follow-ups do not install an app, force-stop
Chrome or relaunch the locked app. They start from the current screen and pass
the complete chat history to the next turn's agent.

The UI collapses each turn's existing evidence viewer under that turn. Evidence
requests always use the turn ID, not the chat-session ID. Preview mutation routes
are disabled; headless visual evidence uses synthetic chat and run fixtures.
