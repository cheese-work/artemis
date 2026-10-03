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
  const latestSteps = new Map<string, { step: Record<string, unknown>; index: number }>();
  logs.forEach((log, index) => {
    if (!log || typeof log !== 'object') return;
    const event = log as Record<string, unknown>;
    const data = event['data'];
    if (['step_updated', 'step_recorded', 'step'].includes(String(event['type']))
      && data && typeof data === 'object') {
      const step = data as Record<string, unknown>;
      latestSteps.set(stepKey(step, index), { step, index });
      return;
    }
    if (event['step_number'] !== undefined || event['step_id'] !== undefined) {
      latestSteps.set(stepKey(event, index), { step: event, index });
    }
  });

  const failingEntry = Array.from(latestSteps.values())
    .sort((left, right) => right.index - left.index)
    .find(({ step }) => isUnresolvedFailure(step));
  if (!failingEntry) return 'Not reported';

  const failedStep = failingEntry.step;
  const action = actionForStep(failedStep);
  const actionName = action && (action['action'] || action['name'] || action['type']);
  const stepNumber = Number(failedStep['step_number']);
  const prefix = Number.isFinite(stepNumber) ? `Step ${stepNumber}` : 'Step';
  return `${prefix}: ${singleLine(typeof actionName === 'string' ? actionName : 'Action failed')}`;
}

function stepKey(step: Record<string, unknown>, index: number): string {
  const stepId = step['step_id'];
  if (stepId !== undefined && stepId !== null && String(stepId).trim()) {
    return `id:${String(stepId)}`;
  }
  const stepNumber = step['step_number'];
  if (stepNumber !== undefined && stepNumber !== null && String(stepNumber).trim()) {
    return `number:${String(stepNumber)}`;
  }
  return `index:${index}`;
}

function isUnresolvedFailure(step: Record<string, unknown>): boolean {
  const status = String(step['status'] ?? '').toLowerCase();
  if (['completed', 'complete', 'success', 'succeeded', 'fixed'].includes(status)) return false;

  const result = parseExecutionResult(step['last_execution_result']);
  const resultStatus = String(result?.['status'] ?? '').toLowerCase();
  if (
    result?.['repair_status'] === 'fixed'
    || ['completed', 'complete', 'success', 'succeeded'].includes(resultStatus)
    || result?.['success'] === true
    || result?.['is_successful'] === true
  ) {
    return false;
  }

  const action = actionForStep(step);
  const actionStatus = String(action?.['status'] ?? '').toLowerCase();
  if (['completed', 'complete', 'success', 'succeeded'].includes(actionStatus) || action?.['success'] === true) {
    return false;
  }
  return isActionFailed(action, step);
}

function parseExecutionResult(value: unknown): Record<string, unknown> | null {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
    } catch {
      return null;
    }
  }
  return value && typeof value === 'object' ? value as Record<string, unknown> : null;
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

  try {
    const url = new URL(trimmed, window.location.origin);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return url.href.replace(/[()]/g, character => encodeURIComponent(character));
  } catch {
    return null;
  }
}

function singleLine(value: string): string {
  return value.replace(/[\r\n]+/g, ' ').trim();
}
