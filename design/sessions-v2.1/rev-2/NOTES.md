## What changed vs rev-1

- Cheese, 13 Oct 2026 (DM, recorded on CHE-1601): "approve direction C — Inspector" and fix the reviewer's blocker. Rev-2 carries C only; A and B stop at rev-1.
- Step rows: each row is now a native `<button>` inside its `<li>` (was a clickable `<li>`). Tab reaches steps 1–7 in order, then the composer; Enter / Space selects. The selected row carries `aria-current="true"` and `aria-controls="evidence"`.
- Focus: a 3 px `--color-focus` ring drawn inside the row (the list clips overflow), shown on step 4 in the captures. Icon ligatures and the "Thinking" hint are `aria-hidden`, so a row reads as "4 Tap first result … After step 4: …". The running step 8 stays a plain row until its evidence exists.
- Inspector: a named region (`#evidence`) whose title is a polite live region and a focus target. Phone bottom sheet keeps the screen text and tapped element (rev-1 dropped them; reviewer, Density) and labels its before/after images.
- New "Keyboard" state card documents Tab, Enter / Space, and the phone sheet's focus-in / Esc-returns-focus contract. Pinned-icons nav and inspector layout unchanged.

## Directions

- C — Inspector: compact action rows as buttons; the right sidebar is an Evidence inspector (before | after, thinking, screen text) with cost in its footer; picker fused as the composer's left segment; Phones as list + detail.

## Limits

- Same as rev-1: system fallback fonts, CSS stand-in phone screens, illustrative cost figures, full-page PNGs (Playwright 1.55.0, system Chrome).
- Static mock: selecting another row does not change the inspector content. The keyboard check is Tab order and accessible names only (Playwright, `ol.steps` aria snapshot).
