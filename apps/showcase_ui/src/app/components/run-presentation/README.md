## Shared run presentation (U1)

These standalone attribute components render on the existing elements. They
do not add layout wrappers, inject run services or own navigation, playback,
clipboard, pin, download or trust-dialog state.

- `appRunStatusBadge`: stored/live status inputs; uses the PR #81 status mapper.
  The viewer keeps its outcome icon and tone. Stream cards keep their compact
  badge and hidden running dot.
- `appRunDeviceLabel`: serial, recorded device, browser ownership and detail
  inputs; uses `runDeviceLabel`. Detail mode preserves the viewer's raw serial.
- `appRunStepRow`: title, step number, failed outcome, optional duration and
  failure detail. Phase mode preserves the stream's Worked/Checked captions
  and projects its existing token details.
- `appRunEvidencePanel`: mapped recording state, video URL, screenshot fallback,
  copy and retry inputs. Player events and the native player reference return
  to the viewer controller. Trigger mode preserves the workspace recording
  launcher; the existing floating player still owns workspace playback.
- `appRunActionBar`: ordered plain action descriptors, feedback and failure
  inputs. Emits the action ID and original event. The existing copy components
  retain clipboard redaction, feedback and manual-copy fallback behavior.

The existing screen controllers keep asynchronous request cancellation,
segmented seeks, authorization, confirmation dialogs and focus restoration.
Styles for generated children move with their component. Host layout and
screen-specific styles remain on the screens.

`run-viewer.component.spec.ts` characterizes rendering before the extraction.
`run-presentation.components.spec.ts` covers both badge presentations, all
recording states, device labels, step details, launcher priority and action
events. The existing viewer and copy suites cover controller behavior.
