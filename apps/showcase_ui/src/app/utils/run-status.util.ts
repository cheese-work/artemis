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

const VIEWS: Record<Exclude<RunStatusKey, 'unknown'>, Omit<RunStatusView, 'key'> & { summaryLabel?: string }> = {
  pending: { label: 'Queued', summaryLabel: 'Pending', icon: 'schedule', tone: 'neutral', active: true },
  running: { label: 'Running', icon: 'play_circle', tone: 'neutral', active: true },
  paused: { label: 'Paused', icon: 'pause_circle', tone: 'neutral', active: true },
  completed: { label: 'Passed', summaryLabel: 'Completed', icon: 'check_circle', tone: 'ok', active: false },
  failed: { label: 'Failed', icon: 'cancel', tone: 'danger', active: false },
  interrupted: { label: 'Interrupted', summaryLabel: 'Unknown', icon: 'warning', tone: 'warn', active: false },
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
  if (!Object.hasOwn(VIEWS, key)) return UNKNOWN;
  const { summaryLabel, ...view } = VIEWS[key as keyof typeof VIEWS];
  return { key, ...view };
}

export function runSummaryStatusLabel(status: string): string {
  const raw = status.toLowerCase();
  const view = runStatusView(raw === 'error' ? 'failed' : raw === 'queued' ? 'pending' : raw);
  return view.key === 'unknown' ? view.label : VIEWS[view.key].summaryLabel ?? view.label;
}

/**
 * A session's status for the Workspace panels. A row the server has not given a status yet is the
 * live run when this tab is watching it. A stored status the UI does not recognise stays Unknown,
 * because the server did say something.
 */
export function sessionStatusView(
  stored: string | null | undefined,
  liveStatus: string | null
): RunStatusView {
  if (stored) {
    return runStatusView(stored);
  }
  return liveStatus === 'running' || liveStatus === 'paused' ? runStatusView(liveStatus) : UNKNOWN;
}
