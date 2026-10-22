## What changed vs rev-2

- Cheese selected Direction C: “i choose design direction C” (`01a1582e-46d8-7d81-a19f-f8ee0930e00a`); selection is not design approval.
- N1: at widths below 800 px, Runs history and run detail are separate hash-navigated views with a visible return link; each direction has separate 390 px history and detail captures.
- N2: Blocked uses the same 32/40 px display type as Fail.
- N3: copied feedback starts hidden, appears only after clipboard success, and is outside the verdict's sole polite live region.
- The SmartQA brand link has a 44 px minimum width and height, matching the design target.

## Finding dispositions

- N1 — Resolved in the static prototype. At 390 px the default view shows Runs history; selecting the run opens its detail view, and the detail view returns to history. The two phone captures show those distinct states, not crops of one stacked document.
- N2 — Resolved. The Blocked verdict uses `var(--text-display)` (32 px) and a 40 px line height, matching Fail.
- N3 — Resolved in markup. Verdict is the only `aria-live="polite"` region. Copy feedback has no live-region role, is `aria-hidden`, and remains hidden until clipboard write succeeds.
- Brand target — Resolved. `.brand` has a minimum 44 × 44 px target at all widths.

## Directions

- A — Report: compare the verdict summary and decisive evidence in a balanced two-column reading surface.
- B — Next action: read the verdict and full-width action first, then inspect evidence without a reserved blank column.
- C — Evidence inspector: keep a compact verdict rail in view while moving through the selected step and evidence; this is Cheese's selected direction.

## Capture and verification

- Captures were rendered on X99 with Chrome 150 at 1×: desktop 1440 × 900 and phone 390 × 844 CSS px. Phone history is the default view (also selected by `#runs`); each detail view uses `#run-detail`.
- The attached C previews are `C-1440.png`, `C-390-history.png`, and `C-390-detail.png`. A and B have matching captures in this revision for comparison.
- The screenshots were opened and visually inspected. All nine captures are non-blank; the phone captures show separate history and detail surfaces.
- No production UI code or external assets are included. Run records are synthetic.
- Browser zoom at 200%, full keyboard operation, screen-reader output, Vietnamese copy review, live baseline and clipboard interaction are NOT-RUN. Contrast pixel sampling is NOT-RUN.
- Cheese's selection does not clear the UI-admission gate or approve the design. PR #220 remains draft and pending merge.
