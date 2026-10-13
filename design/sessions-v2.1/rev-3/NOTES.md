## What changed vs rev-2

- Cheese, 13 Oct 2026 (DM, recorded on CHE-1601): "design bị lỗi icon. icon biến thành text rất dở" — the icons render as text.
- Cause: rev-2 `tokens.css` loaded the Material Symbols font from a repo-relative path (`../../../apps/showcase_ui/public/fonts/…`). It resolved inside the artemis checkout (X99 captures), but not in Open Design, so the `.ms` ligatures showed as words (`space_dashboard`, `smartphone`, `lock`, …).
- Fix: `tokens.css` embeds the same 44,248-byte `material-symbols-outlined.woff2` subset as a `data:` URL. The rev-3 folder now has no path outside itself and no network fetch.
- No layout, copy or state changes: `C.html` and `mock.css` are byte-identical to rev-2.

## Directions

- C — Inspector: unchanged from rev-2 (compact action rows as buttons; Evidence inspector in the right sidebar with cost in its footer; picker fused as the composer's left segment; Phones as list + detail).

## Limits

- Same as rev-2: system fallback fonts for Inter / JetBrains Mono, CSS stand-in phone screens, illustrative cost figures, static mock (selecting another row does not change the inspector).
- Captures: Playwright 1.55.0 full page, system Chrome, rendered from a temp directory that holds only the rev-3 files (no repo access).
