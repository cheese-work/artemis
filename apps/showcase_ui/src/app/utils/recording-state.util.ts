export type Capture = 'pending' | 'recording' | 'stopped' | 'partial' | `missing:${string}` | null;
export type Transfer = 'waiting_for_computer' | 'uploading' | 'uploaded' | 'failed' | null;
export type Playback =
  | 'recording'
  | 'finalizing'
  | 'processing'
  | 'ready'
  | 'failed'
  | 'unavailable'
  | null;

export const RECORDING_STATES = [
  'in_progress',
  'pending',
  'waiting_for_computer',
  'uploading',
  'upload_failed',
  'preparing',
  'prepare_failed',
  'uploaded_unchecked',
  'ready',
  'partial',
  'missing',
  'none',
  'unknown'
] as const;
export type RecordingState = (typeof RECORDING_STATES)[number];

export interface RecordingView {
  state: RecordingState;
  /** Player-area copy. */
  copy: string;
  /** Short label for a list row. */
  badge: string;
  /** Only true when playback is ready and nothing upstream is still in flight. */
  playable: boolean;
  partial: boolean;
  ribbon: string | null;
}

const MISSING_REASONS: Record<string, string> = {
  not_ready: 'the phone was not ready to record.',
  spool_full: 'the computer ran out of space to record.',
  recorder_failed: 'the recorder stopped unexpectedly.',
  device_offline: 'the phone went offline.',
  host_disconnected: 'the computer lost its connection.',
  disabled: 'recording was turned off for this run.'
};

const view = (
  state: RecordingState,
  copy: string,
  badge: string,
  extra: Partial<Pick<RecordingView, 'playable' | 'partial' | 'ribbon'>> = {}
): RecordingView => ({ state, copy, badge, playable: false, partial: false, ribbon: null, ...extra });

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * The one place recording copy comes from. Capture (did the phone record?),
 * transfer (did the video reach the server?) and playback (can the server play
 * it?) are separate facts, and only playback "ready" makes a video watchable:
 * an upload that finished says nothing about readiness.
 */
export function mapRecording(
  input: { capture: Capture; transfer: Transfer; playback: Playback },
  options: { stoppedAtSeconds?: number | null } = {}
): RecordingView {
  const { capture, transfer, playback } = input;

  if (capture?.startsWith('missing:')) {
    const reason = MISSING_REASONS[capture.slice('missing:'.length)] ?? 'the reason was not reported.';
    return view('missing', `No recording: ${reason}`, 'No video');
  }
  if (capture === 'recording') return view('in_progress', 'Recording in progress', 'Recording');
  if (capture === 'pending') {
    return view('pending', 'The recording has not started yet.', 'Video pending');
  }

  if (transfer === 'waiting_for_computer') {
    return view('waiting_for_computer', 'Video will appear when your computer reconnects.', 'Video waiting');
  }
  if (transfer === 'uploading') return view('uploading', 'Uploading video…', 'Video uploading');
  if (transfer === 'failed') {
    return view(
      'upload_failed',
      'Video upload failed. Retry upload runs from the computer.',
      'Upload failed'
    );
  }

  // Transfer is unknown (browser or older run) or finished: playback decides.
  switch (playback) {
    case 'ready': {
      if (capture === 'partial') {
        const at = options.stoppedAtSeconds;
        const ribbon = at == null ? 'Partial recording' : `Partial recording (stopped at ${clock(at)})`;
        return view('partial', ribbon, 'Partial video', { playable: true, partial: true, ribbon });
      }
      return view('ready', 'Recording ready.', 'Video ready', { playable: true });
    }
    case 'recording':
    case 'finalizing':
    case 'processing':
      return view('preparing', 'Preparing video…', 'Preparing video');
    case 'failed':
      return view('prepare_failed', 'The video could not be prepared.', 'Video failed');
    case 'unavailable':
      return transfer === 'uploaded'
        ? view('preparing', 'Preparing video…', 'Preparing video')
        : view('none', 'No recording for this run.', 'No video');
    default:
      return transfer === 'uploaded'
        ? view('uploaded_unchecked', 'Video uploaded. Checking that it can play…', 'Video uploaded')
        : view('unknown', 'Recording status unknown.', 'Video unknown');
  }
}

/** Player-area copy for a video that could not be prepared: the fixed sentence plus the reported reason. */
export function prepareFailedCopy(base: string, message: string | null | undefined): string {
  return `${base} ${message?.trim() || 'The video service did not report a reason.'}`;
}

/** Raw recorder output for the collapsed "Technical details" block; null when there is none worth showing. */
export function technicalDetail(detail: string | null | undefined): string | null {
  return detail?.trim() || null;
}

/**
 * The ribbon over a live run's recording when the run was interrupted: the video stops where
 * the phone was lost. Null for any other status.
 */
export function partialRibbonFor(
  session: { status?: string; start_time?: number; end_time?: number } | null | undefined
): string | null {
  if (session?.status?.toLowerCase() !== 'interrupted') return null;
  const stoppedAt = session.end_time != null && session.start_time ? session.end_time - session.start_time : null;
  return mapRecording({ capture: 'partial', transfer: null, playback: 'ready' }, { stoppedAtSeconds: stoppedAt }).ribbon;
}
