## What changed vs the live run view

- A — Report: gives the verdict summary and decisive step equal weight in a two-column review layout.
- B — Next action: keeps the same reading order and gives the backend-provided action a broad, quiet emphasis.
- C — Evidence inspector: keeps the verdict summary in a compact rail beside the selected step and screenshot.
- The anonymous X99 route redirected to Cloudflare Access; a signed-in run view was not captured to avoid copying production QA data.
- The three directions use synthetic run data and the existing app tokens; labels and actions shown are backend response examples, not a frontend catalog.

## Directions

- A — Report: scan the result, jump to the cited step, and compare expected with actual.
- B — Next action: keep cause and evidence first, then make the next step easy to find.
- C — Evidence inspector: compare the selected step and its screenshot without losing the result summary.

## Checks

- X99 captures: 390 / 800 / 1440 CSS px; 200%-zoom-equivalent at a 720 × 450 CSS viewport with 2× device scale (1440 × 900 output; browser zoom was not separately toggled).
- Synthetic states: loading, empty, pending, load error, cancelled, legacy, active-run hint, and one Blocked card.
- Focus ring, 44 px controls, wrapping labels, and responsive stacking are represented in the static mockups.
- Live run-detail baseline: NOT-CAPTURED; `/sessions/new` requires Cloudflare Access.
