## What changed vs rev-3

- F1: unrelated steps 5 and 6 now start collapsed in the C inspector; each remains expandable through its native summary control.
- F2: replace the 1440 × 900 viewport crops with exact-revision full-page A/B/C captures that include every state example; regenerate separate 390 px C history and detail captures.
- F3: make the real search input 44 px high and the retry link at least 44 × 44 px.
- “F1 goes to rev-4, not a deferred admit-sheet Change. Direction C remains selected.” — Sol, decision comment `01a1587e-ba68-72d0-a641-5aeb4cfa5151`.

## Finding dispositions

- F1 — Resolved in the selected C prototype. Steps 5 and 6 are closed by default on phones, so their contents no longer push the selected failing step below two expanded unrelated steps. The summaries remain available to reveal prior steps.
- F2 — Resolved with `A-full.png`, `B-full.png`, and `C-full.png`, captured from this revision at 1440 CSS px and showing all state examples. `C-390-history.png` and `C-390-detail.png` are separate 390 × 844 CSS px captures of the history and detail hash views at the same revision.
- F3 — Resolved in the shared stylesheet. The actual search `<input>` has a 44 px height; the retry anchor has 44 px minimum width and height. Retained minor finding: below 800 px, top icon navigation and bottom tabs repeat the same four destinations.

## Directions

- A — Report: compare the verdict summary and decisive evidence in a balanced two-column reading surface.
- B — Next action: read the verdict and full-width action first, then inspect evidence without a reserved blank column.
- C — Evidence inspector: keep a compact verdict rail in view while moving through the selected step and evidence; this remains Cheese's selected direction from rev-3, not design approval.

## Capture and verification

- Captures are from the rev-4 files on X99 with Chrome 150 at 1×. Desktop full pages use 1440 CSS px; C phone views use 390 × 844 CSS px.
- The five PNGs were opened and checked for non-blank content. The three full-page images show the loading, empty, pending, error, cancelled, legacy, blocked, and active states.
- Browser zoom at 200%, full keyboard operation, screen-reader output, Vietnamese copy review, live baseline, clipboard interaction, and contrast pixel sampling remain NOT-RUN. Run data is synthetic. No production UI code is included.
- F3's duplicate navigation is retained as a minor finding, not represented as resolved. Cheese's direction selection remains selection only; PR #220 remains draft pending fresh independent UX review, design merge, and the separate UI-admission gate.
