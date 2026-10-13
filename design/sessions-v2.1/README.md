## Request

Cheese, 13 Oct 2026 (CHE-1556 follow-up, requirements recorded on CHE-1597, now cancelled as a duplicate of CHE-1601): "very poor UX UI".

- Each step shows the agent **action first**, then the action evidence (the post-action screenshot).
- Agent thinking is hidden in **collapsible, dimmed** text.
- Pre/post screenshots sit in a collapsible screenshot item. Clicking an action screenshot shows the **pre/post comparison**.
- Step screenshots carry meaningful alt text (`ui_tree` / `ocr_result` exist in the `images` table but are not exposed).
- "About to run on which device" moves into the chat composer. Once a session has run, its phone is locked: dimmed, filled connection-status dot, cannot change.
- When the session's phone disconnects: disable the composer, ask to reconnect first, the picker blinks. Opening it shows **Reconnect**, or **run the same initial prompt on another device**, which opens a **new chat session**.
- Phones/Devices becomes a separate page (today phone lists sit inside `/setup`).
- Sidebar nav mode becomes an **icon toggle button**: Pinned expanded → Pinned icons → Expand on hover (today a `<select>`).
- Show session-wide tokens and an **estimated API cost** (today tokens cover the latest turn only; no price data exists).
- "Directions must be judged against a real live session, not only synthetic captures."

Out of scope (plain bugs, ship separately): the scroll-up bug and the device-label fallback.

- Issue: [CHE-1601](mention://issue/01a1339d-47b0-70e1-8700-7f6cb4cdcd29) (parent CHE-1594)
- Pages in scope: `sessions` (timeline, composer), `phones` (new), app shell nav
- Live baseline: `live/` — Cheese's screenshots of a real session (`cheese-*.png`) and X99 captures of SmartQA `f74f300` at `127.0.0.1:8000` (`*-x99-*.png`, signed-out view)
- Status: rev-1 posted; Cheese picked C (13 Oct 2026, DM). rev-2 = C with keyboard-focusable step rows, awaiting UX score. Open Design gallery: project `artemis-sessions-v2.1` on C00.
