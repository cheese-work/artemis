# Run result and step disclosure regression (CHE-1459)

## Compared revisions and scope

- Last known-good workspace UI: `e93cf6a` (PR #87).
- Regressed `main`: `56edc235feff8cf11da0a8d72a17a2db4c0b0f25`.
- Restored implementation: `b7bd5b97f375431861cf4e3d3099f0bc2475868c`.
- Following evidence-only commits add this report, screenshots and reproduction tools. They do not change application behavior.

The source comparison starts with `git diff e93cf6a origin/main -- apps/showcase_ui`.
The historical workspace used `AgentStreamComponent`; the historical review route already used the simpler `RunViewerComponent`.
PR #89 replaced the workspace stream with the shared `RunViewComponent` without porting its detailed presentation.
The historical report was at the bottom of the stream, not the top. This fix puts the result summary above the evidence grid in both live and review modes, as requested.

This is functional restoration, not the CHE-1456 visual redesign. U1–U5 routes, layout, action order, theme tokens, phone connection and owner controls stay in place.
No device, production run, deployment or merge is part of this work.

## Missing-element inventory

The inventory covers the removed workspace stream and floating player, not just the two reported symptoms.
"Not restored" identifies a remaining old affordance; it is not a claim that all old UI is reinstated.

| Element present at `e93cf6a` | State on regressed `main` | Treatment in this fix |
| --- | --- | --- |
| `output.md` Task Report, rendered markdown/checklist and report findings | No successful-run task report; only a failed-run reason/report disclosure | Restore persisted Task Report in the top summary; keep existing failed-run banner |
| `report_task_status` explanation and expandable report/findings card | Action title only; explanation for successful runs inaccessible except raw logs | Use the latest status explanation when `output.md` is absent; preserve the action payload in step details |
| Checker run outcome, passed/failed/inconclusive/unchecked totals and unmet subgoals | Shared review does not request `/checks`; live checker result blocks are discarded from visible presentation | Restore top counts, findings and unmet subgoals from persisted or current-session live outcome |
| Checkpoint/final-review heading, subject, status, declared items, item verdicts, evidence, suggestions, errors and redo notes | Absent from normal live/review step UI | Restore inside the anchored step; unmatched/legacy checks have a separate Checker output disclosure |
| Collapsible action/tool cards | Only a selectable step title and selected step's screenshots | Add independent step disclosure buttons, `aria-expanded`, `aria-controls` and labelled regions; native nested disclosures |
| Per-step Thought/Work text and chronological interleaving with tools/actions | Live text appears separately below actions; persisted review reasoning absent | Restore chronological text/tool/action details using the existing event sorter; retain the existing live-stream area |
| Checker per-turn Thought/Work transcript and tool calls | Persisted `/checks` streams never fetched in review; live details reduced to text | Reuse the existing ledger-to-stream conversion for review and live checker blocks |
| Action target, input, coordinates, target bounds/class and extra parameters; tool arguments/results | Absent from normal step UI | Restore labelled parameters, status/error/duration, tool output and a raw payload disclosure |
| Per-action/tool before/after screenshots and final report screenshots | Only the selected step-level pair remains | Restore event-specific pairs with the existing image resolver; retain selected-step pair and player synchronization |
| Worked/Checked phase headings, phase duration and token totals | Removed with phase grouping | Step timestamp, duration and token total are available inside details; old phase grouping/phase totals are not restored |
| One-click Copy run summary | Component still exists but is not mounted by RunView | Not restored; Copy link and Download remain unchanged |
| Architecture/model chip; run-info elapsed clock, context percentage meter and Pro result-check/screen-reading tuning labels | Model/tokens/context numbers remain in live Run information; elapsed meter/tuning labels absent | Not restored; current Run information remains unchanged |
| Notes & Plans selector, parsed plan/milestone/check cards, tool-note links and View in Notes | No equivalent session-notes viewer in RunView | Only `output.md` is restored; general notes navigation is not restored |
| Image zoom modal, target/touch overlays and image-click inspection | Step images are plain images | Not restored; image URLs remain inspectable normally |
| Specialized tool presentation: ADB command/cwd/terminal cards, video-analysis cards, failure-recovery cards, LLM retry/compression/backend-switch annotations | Only step/failure title and separate live text remain | Raw action/tool content is available in expanded details; specialized visual cards and annotations are not reinstated |
| Floating player's Video/Steps modes, screenshot replay auto-play, previous/next frames, playback-rate cycling, before/after frame switch, minimize/theater/close and live refresh | Floating player removed; fixed evidence panel has native video and selected screenshots | Not restored; current evidence panel/video behavior remains unchanged |

The old task queue, owner labels, stop action, recording-status copy, startup progress and paused/retrying notices were relocated or redesigned by U1–U5 rather than all being lost. They are not reverted here.

## Verification

Failing-first tests were written before the implementation edits. Chrome's default Karma launch could not capture on this X99 runtime.
The same added tests were then run against a pristine archive of `56edc235`, not a reverse-patched implementation:

- **RED:** 135 tests executed; all 8 new summary/disclosure tests failed; the 127 existing RunView tests passed.
- **GREEN:** 137 RunView tests passed, including two additional isolation/live-checker cases.
- **Full frontend:** 968 tests passed on the restored implementation.
- **Build:** Angular production build passed.
- **Static:** theme lint, UI-string lint and `git diff --check` passed.

The eight baseline failures cover top reports in both modes, independent step expand/collapse in both modes, persisted checker results/reasoning, status-report fallback/escaping, unavailable states and live result updates.
The added isolation cases cover cancelling a previous run's notes/checks, clearing expansion on navigation, and unanchored live checker tools/verdicts.
Existing failure-report, recording/playback, route, owner-scope and AgentService tests remain in the full passing suite.

Browser receipts cover 1440 × 1000 and 390 × 1000 for a finished run and a simulated running session:

- Enter collapses and Space expands the focused native button.
- Chromium's accessibility tree exposes `button`, `Step 1 details`, and `expanded: true`.
- Reports, action/tool output and persisted checker reasoning are present.
- Neither viewport has horizontal document overflow.

These are deterministic local API-fixture screenshots, not authenticated live-site or Android-device evidence.
All three revisions use the same session ID, steps, notes, checker ledger, image and status fixtures.
The fixture intentionally has no recording, so screenshots exercise the retained evidence fallback.

## Screenshots

| State / width | Historical workspace | Regressed main | Restored summary | Restored step details |
| --- | --- | --- | --- | --- |
| Finished / 1440 | [Old](assets/che-1459/old-finished-1440.png) | [Before](assets/che-1459/before-finished-1440.png) | [After](assets/che-1459/after-finished-1440.png) | [Details](assets/che-1459/after-finished-1440-details.png) |
| Finished / 390 | [Old](assets/che-1459/old-finished-390.png) | [Before](assets/che-1459/before-finished-390.png) | [After](assets/che-1459/after-finished-390.png) | [Details](assets/che-1459/after-finished-390-details.png) |
| Live / 1440 | [Old](assets/che-1459/old-live-1440.png) | [Before](assets/che-1459/before-live-1440.png) | [After](assets/che-1459/after-live-1440.png) | [Details](assets/che-1459/after-live-1440-details.png) |
| Live / 390 | [Old](assets/che-1459/old-live-390.png) | [Before](assets/che-1459/before-live-390.png) | [After](assets/che-1459/after-live-390.png) | [Details](assets/che-1459/after-live-390-details.png) |

## Reproduce on X99

From the repository root:

```sh
cd apps/showcase_ui
npm ci
node scripts/validate-whats-new.mjs
node scripts/write-build-info.mjs
CHROME_BIN="$PWD/../../docs/assets/che-1459/chrome-driver.mjs" npx ng test --watch=false --karma-config="$PWD/../../docs/assets/che-1459/karma.cjs"
npm run build
cd ../..
node docs/assets/che-1459/screenshots.mjs apps/showcase_ui/dist/frontend/browser after
```

The evidence launcher uses an isolated Chrome profile, the screenshot runner's known-working flags and CDP navigation after Chrome starts.
It preserves the actual Angular/Karma/Jasmine tests rather than replacing assertions with a mock test runner.
The fixtures bind only loopback, use no credentials, and do not issue device commands.
The screenshot runner stops its Chrome process group, closes the local server and removes the profile before returning.
To recapture the baseline, build a pristine archive at `56edc235` and pass its browser dist path with label `before`.
Build an archive at `e93cf6a` and use label `old` for historical workspace evidence.
