## What changed vs rev-3

- Cheese, 13 Oct 2026, Open Design comments on `rev-3/C.html` (copied to CHE-1601): composer "Quá xấu; Nên để chat text field này 2 dòng" (too ugly; make the chat field 2 lines), and Before image "Có thêm annotation action mà agent đã thao tác" (add an annotation of the action the agent performed).
- Composer: a two-line field on top; the phone chip (status dot, label, lock) and Pause / Stop / Send sit in a toolbar below it. Same layout in the disconnected and new-session states.
- Before screenshot: a marker shows the agent's action (tap ring at the tap point), with a caption under the image ("Tap · 540, 600") and the action in the image label. A new board section shows the four marker kinds: tap, type, swipe, key press.
- Snapshot-safe markup: Open Design's "Screenshot to chat" copies the page and drops web-font icons, `::before` / `::after` and CSS gradients (probe on CHE-1601). Rev-4 draws icons as inline SVG (Material Symbols Outlined, wght 300, extracted from the app's subset font), draws the phone stand-ins as inline SVG, and has no pseudo-element art, so live view and that tool show the same thing. The icon font is no longer loaded.

## Directions

- C — Inspector: rev-3 layout with the two-line composer and action markers on the Before screenshot.

## Limits

- Same as rev-3: system fallback fonts for Inter / JetBrains Mono, SVG phone stand-ins instead of real screenshots, illustrative cost figures and tap coordinates, static mock.
- Captures: Playwright 1.55.0 full page, system Chrome. Rev-2 reviewer note still open: step labels truncate at 390 px.
