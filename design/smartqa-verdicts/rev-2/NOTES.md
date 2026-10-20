## What changed vs rev-1

- Added responsive navigation, account access, and the complete Runs history at 390 px; retained the run list and added the bottom tab bar below 800 px.
- Captured each direction as a full page and at 390, 800, and 1440 CSS px. The full-page captures show the loading, empty, pending, error, cancelled, legacy, blocked, and active states.
- Raised each verdict word from a 13 px chip to the `DESIGN.md` display style (32/40). Added 44 px minimum height to every run-card evidence link.
- Rebuilt the three layouts as distinct choices: A balances report and evidence; B gives the action the full first row and uses the full evidence width; C pins a compact verdict rail beside a step inspector.
- Answered Sonnet's rev-1 verdict `01a157bc-eeae-7a67-84f8-e2ae8e6405e5`: blocker 1, mobile nav/account/history are present; blocker 2, all directions have full-page captures; blocker 3, evidence links meet 44 px; blocker 4, B has no empty evidence column.

## Remaining findings and dispositions

- Blocked card order, turn result, backend-provided verification label, and copy feedback: fixed in all directions. The card now shows verdict and verification, reason, evidence, action, responsible party, turn result, then ID/help controls.
- Runs history lacks title, time, or device: fixed with a distinct synthetic title, timestamp, and device label on each card. Every history card follows verdict/verification → reason → evidence → action → responsible → turn result; run metadata follows the result.
- Vietnamese page language and English static labels: fixed with `lang="vi"` and Vietnamese interface labels. Product verdict values remain Pass, Fail, and Blocked.
- Missing active state and hint outside the run header: fixed. The active example names `Đang chạy — chưa có kết quả` and places `Giữ tab mở, không khoá máy` in its run header.
- Duplicate `h1` and nested step heading: fixed. Each page has one `h1`; the evidence panel is `h2`; the selected step is `h3`.
- Lightbox has no interaction contract: markup uses a native dialog element. Implementation must open it from the screenshot trigger, keep focus in the modal, close on Escape, and return focus to the trigger. Static HTML does not exercise these interactions.
- A/B/C offer too little choice: revised the report's balanced split (A), action-first full-width composition (B), and sticky verdict rail with step index (C). Direction C's out-of-scope App / Phone / Model / Duration facts are removed.
- Direction-specific hierarchy and density findings: A keeps report and evidence balanced without a dead column; B gives the action priority and uses the evidence panel's full width; C keeps the verdict rail beside the selected evidence. Fresh independent scores remain pending.
- 200% browser zoom, complete keyboard operation, and screen-reader output: NOT-RUN. The 720 CSS px capture is a responsive-width check, not a claim of browser zoom. A Tab check verified the visible 3 px focus ring on A at 390 px. The new UX reviewer should verify zoom and interaction behavior.
- Live run-view baseline and Vietnamese copy review: NOT-CAPTURED / NOT-RUN. The previous X99 attempt redirected the anonymous route to Cloudflare Access; no signed-in run or production QA data was opened. All displayed run data is synthetic.
- Contrast pixel sampling: NOT-RUN. Rev-2 preserves rev-1's color tokens; Sonnet's token-based contrast calculations passed 4.5:1.

## Directions

- A — Report: compare the verdict summary and decisive evidence in a balanced two-column reading surface.
- B — Next action: read the verdict and full-width action first, then inspect evidence without a reserved blank column.
- C — Evidence inspector: keep a compact verdict rail in view while moving through the selected step and evidence.

## Capture and behavior notes

- X99 full-page PNGs: `A-full.png`, `B-full.png`, `C-full.png`. Viewport PNGs: each direction at 390, 720, 800, and 1440 CSS px. The 720 px capture is only a responsive-width check.
- Playwright 1.56.1 with Chrome 150 checks all three directions at 390, 720, 800, and 1440 px: no horizontal overflow, one `h1`, all eight state cards, visible run history, and 44 px run links and account targets. A keyboard Tab check at 390 px shows the 3 px focus ring.
- All captures use synthetic records and local HTML/CSS. No external assets or production UI code are included.
- Evidence links must select the cited turn and step, expand the step, scroll it into view, and move focus to it. The screenshot trigger opens the modal dialog described above.
- State and verdict announcements use the visible backend label; verification and catalog labels are backend-provided values, not frontend catalog translations.
