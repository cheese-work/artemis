## Shared run presentation (U1)

These standalone attribute components render on the existing elements. They
do not add layout wrappers, inject run services or own navigation, playback,
clipboard, pin, download or trust-dialog state.

- `appRunStatusBadge`: stored/live status inputs; uses the PR #81 status mapper.
  The viewer keeps its outcome icon and tone. Stream cards keep their compact
  badge and hidden running dot.
- `appRunDeviceLabel`: serial, recorded device, browser ownership and detail
  inputs; uses `runDeviceLabel`. Detail mode preserves the viewer's raw serial.
- `appRunStepRow`: 56 px rows with an action icon, 56 × 40 thumbnail, title,
  step number, action kind, mono duration or elapsed time, and failed outcome.
  Missing or broken thumbnails retain their fixed box. Phase mode preserves the stream's Worked/Checked captions
  and projects its existing token details.
- `appRunEvidencePanel`: mapped recording state, video URL, screenshot fallback,
  copy and retry inputs. Player events and the native player reference return
  to the viewer controller. Trigger mode preserves the workspace recording
  launcher; the existing floating player still owns workspace playback.
- `appRunActionBar`: ordered plain action descriptors, feedback and failure
  inputs. Emits the action ID and original event. The existing copy components
  retain clipboard redaction, feedback and manual-copy fallback behavior.

The status table also retains the summary's legacy captions (`Completed`,
`Pending`, and `Unknown` for an interrupted run). Summary-only `error` and
`queued` aliases do not change badge status handling. This extraction does
not change copied text; caption unification is a separate behavior change.

The existing screen controllers keep asynchronous request cancellation,
segmented seeks, authorization, confirmation dialogs and focus restoration.
Styles for generated children move with their component. Host layout and
screen-specific styles remain on the screens.

`run-view.component.spec.ts` retains the characterization tests from before the extraction.
`run-presentation.components.spec.ts` covers both badge presentations, all
recording states, device labels, step details, launcher priority and action
events. The existing viewer and copy suites cover controller behavior.

`RunViewComponent.selectedStep` is the shared source for timeline highlighting
and the evidence pane. Verdict links call `goToStep(stepNumber)` to select,
scroll to and focus an existing step without changing selection for a missing
step. The independent 44 px toggle reveals execution details and before/after
screenshots without selecting the row. Arrow Up/Down, Home and End select and
focus timeline rows; native Tab, Enter and Space behavior is unchanged.

`npm run test:step-timeline` reuses the headless run-header fixture audit with
a finished failed run at 1440 px and 390 px. The audit checks geometry,
selection, keyboard navigation, details and the verdict-jump hook, and writes
PNG screenshots plus `results.json`. The screenshots are mocked API evidence,
not Android-device or live-backend evidence. Karma specs run in GitHub CI.

## Unified controller (U2)

`RunViewComponent` now owns the run layout on both Workspace and `/runs/:id`.
The Workspace supplies the selected session, consolidated streaming steps and
phone preparation progress. The catalog remains the source of finished-run
status, owner, device and recording metadata. Completion refreshes evidence
without replacing the component or clearing the selected step.

`RunsService.viewPosition` retains the last run's selection and both scroll
positions across route changes. Both modes expose failed-step reasons and
before/after screenshots, and retain the same trust dialogs and action bar.
Only Workspace mounts the new-task box. The keyboard walkthrough runs its
same timeline and action assertions in both modes using trusted Chrome keys.
