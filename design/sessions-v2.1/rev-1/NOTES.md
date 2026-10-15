## What changed vs live (`f74f300`, rev-0 = `../live/`)

- Step row: was a bare "Step N" + thumbnail behind a `<details>`; now the action sentence comes first, then the post-action screenshot, thinking folds into a dimmed toggle, and a click shows before/after with OCR / UI-tree text as alt text.
- Phone: moves from the nav chip ("Pixel 6 Pro · Phone 127.0.0.1:35773") into the composer, locked after the first run, with a status dot; disconnect disables the composer, blinks the picker and offers Reconnect or a new session on another phone.
- Phones: new top-level page out of `/setup`, with status, current session and Reconnect per phone.
- Nav mode: the "Pinned expanded" `<select>` becomes one 44 px icon button that cycles three modes, with a 3-dot position marker.
- Usage: session-wide tokens plus "≈ $ est." at list price, with a "cost —" state when no price exists (today: latest-turn tokens in the Stats tab only).

## Directions

- A — Ledger: text-first rows; post-action thumbnail on the row's right; before/after opens inline; cost line under the title; picker at the left of the composer actions; Phones as a flat table.
- B — Filmstrip: screenshot-first card strip per turn; before/after in a dialog with step navigation; composer status strip (phone left, cost right); Phones as device cards.
- C — Inspector: compact action rows; the right sidebar becomes an Evidence inspector (before | after, thinking, screen text) with cost in its footer; picker fused as the composer's left segment; Phones as list + detail.

## Limits

- Fonts: Inter and JetBrains Mono are not fetched (no network); captures use the system fallback.
- Phone screenshots are CSS stand-ins, not real step images; the step texts follow Cheese's real session ("open youtube and play gangnam style at maximum speaker volumn").
- Cost figures and model price are illustrative; no price table exists in SmartQA yet.
- PNGs are full-page captures (Playwright 1.55.0, system Chrome) so every state is visible.
