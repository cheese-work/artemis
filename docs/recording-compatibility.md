# Recording compatibility (CHE-1264)

Recording requires scrcpy 2.4 or newer. This is a conservative recorder-wide
floor, not a per-device Android-version probe or a guarantee that every newer
Android release is supported. It replaces the previous 1.25 floor.
The scrcpy 2.4 release includes adaptations for Android 15's display API.

The failing run `22d8d458` on October 7, 2026 logged both a clipboard listener
`NoSuchMethodException` and a fatal `android.view.SurfaceControl.createDisplay`
exception. Disabling clipboard autosync does not address the display API call.
Both exception signatures now classify as `recorder_incompatible`.

Readiness rejects older scrcpy with
`scrcpy too old for this phone's Android version`. Recording startup emits
`No recording: <reason>` before automation begins. Generic errors use at most
200 characters from the first line; existing logs retain the complete error.
The startup notice follows task submission, not a pre-submission readiness gate.

On Linux x86_64, `start.sh` selects an existing supported binary or fetches the
official portable scrcpy 4.1 archive. The download is SHA-256 checked before
extraction and the executable version is checked before selection. The versioned
user-space directory is `~/.local/share/scrcpy/4.1`. An old cached binary or old
`ARTEMIS_SCRCPY_PATH` does not bypass the fallback. Failed downloads remain
unavailable, and unsupported architectures receive a separate-install notice.

Do not silently upgrade the shared X99 host. Its service owner must arrange the
host-side installation and explicit recorder selection as a separate step.
Source checks do not establish phone compatibility. Same-phone validation remains
NOT-RUN until scoped Android admission; Cheese's real run is the final acceptance
check. The PR must remain draft until fresh independent review and OCR disposition.
