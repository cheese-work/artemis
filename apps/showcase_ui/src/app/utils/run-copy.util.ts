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
  const latestSteps: { step: Record<string, unknown>; index: number }[] = [];
  const stepsById = new Map<string, { step: Record<string, unknown>; index: number }>();
  const stepsByNumber = new Map<number, { step: Record<string, unknown>; index: number }>();
  logs.forEach((log, index) => {
    if (!log || typeof log !== 'object') return;
    const event = log as Record<string, unknown>;
    const data = event['data'];
    if (['step_updated', 'step_recorded', 'step'].includes(String(event['type']))
      && data && typeof data === 'object') {
      const step = data as Record<string, unknown>;
      mergeStep(step, index, latestSteps, stepsById, stepsByNumber);
      return;
    }
    if (event['step_number'] !== undefined || event['step_id'] !== undefined) {
      mergeStep(event, index, latestSteps, stepsById, stepsByNumber);
    }
  });

  const failingEntry = latestSteps
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

function mergeStep(
  step: Record<string, unknown>,
  index: number,
  latestSteps: { step: Record<string, unknown>; index: number }[],
  stepsById: Map<string, { step: Record<string, unknown>; index: number }>,
  stepsByNumber: Map<number, { step: Record<string, unknown>; index: number }>
): void {
  const stepId = step['step_id'] === undefined || step['step_id'] === null
    ? ''
    : String(step['step_id']).trim();
  const rawNumber = step['step_number'];
  const stepNumber = rawNumber === undefined || rawNumber === null || String(rawNumber).trim() === ''
    ? null
    : Number(rawNumber);
  const existing = (stepId && stepsById.get(stepId))
    || (stepNumber !== null && Number.isFinite(stepNumber) ? stepsByNumber.get(stepNumber) : undefined);

  if (existing) {
    existing.step = {
      ...existing.step,
      ...step,
      generic_tools: mergeGenericTools(existing.step['generic_tools'], step['generic_tools'])
    };
    existing.index = index;
  } else {
    latestSteps.push({ step: { ...step }, index });
  }

  const target = existing || latestSteps[latestSteps.length - 1];
  if (stepId) stepsById.set(stepId, target);
  if (stepNumber !== null && Number.isFinite(stepNumber)) stepsByNumber.set(stepNumber, target);
}

function mergeGenericTools(existing: unknown, incoming: unknown): unknown[] {
  const merged = Array.isArray(existing) ? [...existing] : [];
  if (!Array.isArray(incoming)) return merged;
  for (const tool of incoming) {
    const traceId = tool && typeof tool === 'object'
      ? String((tool as Record<string, unknown>)['trace_id'] ?? '')
      : '';
    const existingIndex = traceId
      ? merged.findIndex((item) => item && typeof item === 'object'
        && String((item as Record<string, unknown>)['trace_id'] ?? '') === traceId)
      : -1;
    if (existingIndex === -1) {
      merged.push(tool);
    } else {
      merged[existingIndex] = { ...(merged[existingIndex] as object), ...(tool as object) };
    }
  }
  return merged;
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
  if (Array.isArray(step['generic_tools'])) {
    const failedTool = [...step['generic_tools']].reverse().find(tool =>
      !!tool && typeof tool === 'object' && isActionFailed(tool)
    );
    if (failedTool && typeof failedTool === 'object') {
      return failedTool as Record<string, unknown>;
    }
  }

  const action = step['action_taken'];
  if (action && typeof action === 'object') {
    return action as Record<string, unknown>;
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
