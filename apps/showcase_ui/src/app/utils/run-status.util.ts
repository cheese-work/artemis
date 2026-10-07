export type RunStatusKey =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'interrupted'
  | 'cancelled'
  | 'unknown';

export type Tone = 'ok' | 'warn' | 'danger' | 'neutral';

export interface RunStatusView {
  key: RunStatusKey;
  label: string;
  icon: string;
  tone: Tone;
  /** Still queued or executing: belongs in the queue, not in history. */
  active: boolean;
}

const VIEWS: Record<Exclude<RunStatusKey, 'unknown'>, Omit<RunStatusView, 'key'>> = {
  pending: { label: 'Queued', icon: 'schedule', tone: 'neutral', active: true },
  running: { label: 'Running', icon: 'play_circle', tone: 'neutral', active: true },
  paused: { label: 'Paused', icon: 'pause_circle', tone: 'neutral', active: true },
  completed: { label: 'Passed', icon: 'check_circle', tone: 'ok', active: false },
  failed: { label: 'Failed', icon: 'cancel', tone: 'danger', active: false },
  interrupted: { label: 'Interrupted', icon: 'warning', tone: 'warn', active: false },
  cancelled: { label: 'Cancelled', icon: 'block', tone: 'neutral', active: false }
};

const UNKNOWN: RunStatusView = { key: 'unknown', label: 'Unknown', icon: 'help', tone: 'neutral', active: false };

/**
 * The one place a stored run status becomes a label. Workspace history, the queue panel and the
 * Runs page all call it, so a state can never read differently on two screens. A missing or new
 * status is Unknown, never Completed.
 */
export function runStatusView(status: string | null | undefined): RunStatusView {
  const raw = (status ?? '').toLowerCase();
  const key = (raw === 'success' ? 'completed' : raw) as RunStatusKey;
  return key in VIEWS ? { key, ...VIEWS[key as keyof typeof VIEWS] } : UNKNOWN;
}

/**
 * A session's status for the Workspace panels. A row the server has not given a status yet is the
 * live run when this tab is watching it; otherwise it is Unknown.
 */
export function sessionStatusView(
  stored: string | null | undefined,
  liveStatus: string | null
): RunStatusView {
  const view = runStatusView(stored);
  return view.key === 'unknown' && (liveStatus === 'running' || liveStatus === 'paused')
    ? runStatusView(liveStatus)
    : view;
}
