## What changed vs rev-4

- F1: Below 800 px, the selected failing-step card now appears before the step index, screenshot, and unrelated step history. The expected and actual text stays expanded in the first detail view.
- F2 and F3 remain resolved from rev-4. Rev-5 regenerates full-page A/B/C captures and separate C phone history/detail captures; the retry and search targets remain 44 × 44 px or larger.
- “F1 goes to rev-5, not acceptance of the current phone layout.” — Sol, decision comment `01a158b1-4bc8-79f8-ad57-4c48a7656e59`.
- Cheese’s direction selection remains “i choose design direction C” (comment `01a1582e-46d8-7d81-a19f-f8ee0930e00a`); rev-5 does not reopen that selection.

## Finding dispositions

- F1 — Resolved in C at widths below 800 px. The selected failing-step details precede the index, screenshot, and unrelated steps. The first 390 × 844 detail capture shows “Mong đợi” and “Thực tế”.
- F2 — Preserved as resolved: exact-revision full-page A/B/C captures include the state examples; C also has distinct phone history and detail captures.
- F3 — Preserved as resolved: the retry link and actual search input retain 44 × 44 px minimum targets. Duplicate navigation remains a retained minor finding.
- F4 — The selected-revision record is updated in `DESIGN.md` and this topic’s README after the rev-5 content commit, with that exact content commit SHA and pending-merge status.

## Directions

- A — Report: compare the verdict summary and decisive evidence in a balanced two-column reading surface; otherwise unchanged from rev-4.
- B — Next action: read the verdict and full-width action first, then inspect evidence without a reserved blank column; otherwise unchanged from rev-4.
- C — Evidence inspector: keep the selected failing-step details first on phones while retaining the selected C direction and the existing desktop layout.

## Capture and verification

- Captures are rendered from rev-5 on X99 with Chrome 150 at 1×. Desktop full pages use 1440 CSS px; phone captures use 390 × 844 CSS px.
- The C phone detail image uses the `#turn-2-step-7` evidence link at 390 × 844 CSS px. The first viewport shows the full selected card, including both “Mong đợi” and “Thực tế” values. The C history image uses the initial history view.
- Browser zoom at 200%, full keyboard operation, screen-reader output, Vietnamese copy review, live baseline, clipboard interaction, and contrast pixel sampling remain NOT-RUN. Run data is synthetic. No production UI code is included.
