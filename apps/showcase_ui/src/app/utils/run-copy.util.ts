import { Session } from '../core/models/session.model';
import { isActionFailed } from './action-formatter.util';

export function buildRunSummary(
  session: Session,
  currentStatus: string,
  logs: unknown[],
  recordingUrl: string | null | undefined
): string {
  const device = session.device_serial || session.device_id || 'Unknown';
  const recording = safeRecordingUrl(recordingUrl);

  return [
    '# Run summary',
    `- Run ID: ${singleLine(session.session_id)}`,
    `- Device: ${singleLine(device)}`,
    `- Outcome: ${formatOutcome(session.status || currentStatus)}`,
    `- Failing step: ${findFailingStep(logs)}`,
    `- Recording: ${recording ? `[Open recording](${recording})` : 'Not available'}`
  ].join('\n');
}

function findFailingStep(logs: unknown[]): string {
  const steps = logs.flatMap(log => {
    if (!log || typeof log !== 'object') return [];
    const event = log as Record<string, unknown>;
    const data = event['data'];
    if (['step_updated', 'step_recorded', 'step'].includes(String(event['type']))
      && data && typeof data === 'object') {
      return [data as Record<string, unknown>];
    }
    if (event['step_number'] !== undefined || event['step_id'] !== undefined) {
      return [event];
    }
    return [];
  });

  const failedStep = steps.reverse().find(step => {
    const action = actionForStep(step);
    return isActionFailed(action, step);
  });
  if (!failedStep) return 'Not reported';

  const action = actionForStep(failedStep);
  const actionName = action && (action['action'] || action['name'] || action['type']);
  const stepNumber = Number(failedStep['step_number']);
  const prefix = Number.isFinite(stepNumber) ? `Step ${stepNumber}` : 'Step';
  return `${prefix}: ${singleLine(typeof actionName === 'string' ? actionName : 'Action failed')}`;
}

function actionForStep(step: Record<string, unknown>): Record<string, unknown> | null {
  const action = step['action_taken'];
  if (action && typeof action === 'object') {
    return action as Record<string, unknown>;
  }

  if (Array.isArray(step['generic_tools'])) {
    const failedTool = step['generic_tools'].find(tool => {
      return !!tool && typeof tool === 'object' && isActionFailed(tool, step);
    });
    if (failedTool && typeof failedTool === 'object') {
      return failedTool as Record<string, unknown>;
    }
  }
  return null;
}

function formatOutcome(status: string): string {
  switch (status.toLowerCase()) {
    case 'success':
    case 'completed':
      return 'Completed';
    case 'failed':
    case 'error':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
    case 'paused':
      return 'Paused';
    case 'pending':
    case 'queued':
      return 'Pending';
    case 'running':
      return 'Running';
    default:
      return 'Unknown';
  }
}

function safeRecordingUrl(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || /[\r\n]/.test(trimmed)) return null;

  const isRelativePath = trimmed.startsWith('/') && !trimmed.startsWith('//');
  const isHttpUrl = /^https?:\/\//i.test(trimmed);
  if (!isRelativePath && !isHttpUrl) return null;

  return trimmed.replace(/[\s()]/g, character => encodeURIComponent(character));
}

function singleLine(value: string): string {
  return value.replace(/[\r\n]+/g, ' ').trim();
}
