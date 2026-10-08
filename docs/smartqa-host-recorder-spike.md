# Host Recorder Spike: scrcpy vs `screenrecord` Segments

Date: 2026-10-04  
Decision owner: CHE-1090 / A2  
Decision for: Release C1 host recorder

## Decision

Use scrcpy on the host as the C1 recorder. Write each closed segment to the host spool and upload it through the resumable chunk protocol. Do not add Android `screenrecord` as a second capture path or fallback in C1.

Start with five-minute scrcpy segments, rotating earlier on display-orientation changes or recorder restarts. Close scrcpy gracefully before publishing a segment; preserve a recoverable partial segment and an explicit status if graceful finalization fails. Keep the accepted 35-minute recording cap and 5 GiB spool cap.

At the planned 2 Mbps default, a five-minute segment is about 75 MB and a full 35-minute recording is about 525 MB. Seven normal segments bound the amount of unclosed video without imposing `screenrecord`'s short segment cadence.

## Comparison

| Concern | Host scrcpy segments | Android `screenrecord` segments |
| --- | --- | --- |
| Capture and file location | Streams from the device and writes the recording directly to the host spool. | Records to device storage; the host must later pull each segment over ADB. |
| Segment cadence | Host can use five-minute segments and rotate earlier for orientation or process recovery. | Artemis currently models a 180-second device-side limit, yielding up to 12 segments for a 35-minute run. |
| Server-network interruption | Capture and local spooling continue while the host-to-server link is unavailable; closed segments can upload later. | Capture can also continue locally, but no segment reaches the host spool until ADB reconnects and the file is pulled. |
| ADB/device-link interruption | The active scrcpy segment may stop or become partial; already closed host segments remain available. | A running device-side segment may continue until stopped or its limit, but collection and cleanup wait for ADB recovery. |
| Recovery and cleanup | Track host-side segment identity, closure, checksum, and upload offset; no device-file janitor is needed. | Track remote paths and orphan processes, enforce device free-space limits, pull and verify files, then delete them safely. |
| C1 integration | Fits the host spool and resumable-upload design with one capture-to-host copy. | Adds remote-file lifecycle and a second ADB transfer before resumable upload. |

## C1 Contract

- Run one host-side scrcpy recorder per recording assignment and retain immutable segment IDs and generation numbers.
- Rotate at five minutes by default, and immediately on orientation change or process recovery; do not silently overwrite an earlier segment.
- Seal and checksum closed segments before upload. Upload with the accepted 8 MiB resumable chunks while capture continues when resources permit.
- On recorder or upload failure, retain host-side bytes until the server's durable acknowledgment and report `partial` or `missing:<reason>` rather than dropping the recording silently.
- Validate the final artifact and exercise host-to-server disconnect/reconnect before changing the segment interval from its initial value.

## Evidence and Limits

This is a design comparison against the accepted C1 architecture and Artemis's existing recording model; it is not a measured device bake-off. No Android device or AVD was used for this memo. The required X99 recording acceptance with scrcpy 1.25 remains NOT-RUN until the Android device squad leader grants an issue-visible exclusive lease to an exact serial.

The first C1 implementation should validate segment finalization, local spool recovery, and resumable upload under a host-to-server disconnect. A later device-side capture experiment is warranted only if pilot evidence shows ADB/device-link loss is a frequent source of missing video and the added remote-file lifecycle is acceptable.
