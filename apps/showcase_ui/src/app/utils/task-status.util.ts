import { Session } from '../core/models/session.model';

export type TaskStatus =
  | 'running'
  | 'paused'
  | 'pending'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'unknown';

/**
 * How the stream and the task list name a session's state. A status this app does not know
 * is `unknown`; it is never shown as `completed`. A session with no status at all is an old
 * row: it counts as running only while the runner reports it so, otherwise as completed.
 */
export function taskStatusOf(
  session: Pick<Session, 'status' | 'session_id'>,
  runner: { sessionId: string | null; status: string }
): TaskStatus {
  const raw = session.status?.toLowerCase();
  if (raw) {
    if (raw === 'success') return 'completed';
    if (['completed', 'failed', 'cancelled', 'interrupted', 'running', 'paused', 'pending'].includes(raw)) {
      return raw as TaskStatus;
    }
    return 'unknown';
  }
  if (session.session_id === runner.sessionId && (runner.status === 'running' || runner.status === 'paused')) {
    return runner.status;
  }
  return 'completed';
}
