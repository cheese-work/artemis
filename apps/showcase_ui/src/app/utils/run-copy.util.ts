import { Session } from '../core/models/session.model';
import { StepBlock } from '../core/models/stream.model';
import { mediaUrl } from './app-url.util';
import { redact } from '../services/logger.service';
import { isActionFailed } from './action-formatter.util';
import { consolidateLogsToBlocks } from './stream-aggregator.util';
import { runSummaryStatusLabel } from './run-status.util';
import { runDeviceLabel } from './device-label.util';

export function buildRunSummary(
  session: Session,
  currentStatus: string,
  logs: unknown[],
  recordingUrl: string | null | undefined
): string {
  const device = runDeviceLabel(session.device_serial || session.device_id || 'Unknown', null, false, true);
  const recording = safeRecordingUrl(recordingUrl);

  return redact([
    '# Run summary',
    `- Run ID: ${singleLine(session.session_id)}`,
    `- Device: ${singleLine(device)}`,
    `- Outcome: ${runSummaryStatusLabel(session.status || currentStatus)}`,
    `- Failing step: ${findFailingStep(logs)}`,
    `- Recording: ${recording ? `[Open recording](${recording})` : 'Not available'}`
  ].join('\n'));
}

function findFailingStep(logs: unknown[]): string {
  const streamEvents = logs.flatMap(log => {
    if (!log || typeof log !== 'object' || Array.isArray(log)) return [];
    const event = log as Record<string, unknown>;
    const data = event['data'];
    const hasData = !!data && typeof data === 'object' && !Array.isArray(data);
    if (hasData && ['step_updated', 'step_recorded', 'step'].includes(String(event['type']))) {
      return [{ ...event, type: event['type'] === 'step' ? 'step_updated' : event['type'] }];
    }
    if (event['step_number'] !== undefined || event['step_id'] !== undefined) {
      return [{ type: 'step_updated', data: event }];
    }
    return hasData ? [event] : [];
  });
  const eventOrder = new WeakMap<StepBlock, number>();
  const failingBlock = consolidateLogsToBlocks(streamEvents, eventOrder)
    .filter(block => block.type === 'step')
    .sort((left, right) => (eventOrder.get(right) ?? -1) - (eventOrder.get(left) ?? -1))
    .find(block => isUnresolvedFailure(block.data));
  if (!failingBlock) return 'Not reported';

  const failedStep = failingBlock.data;
  const action = actionForStep(failedStep);
  const actionName = action && (action['action'] || action['name'] || action['type']);
  const stepNumber = Number(failedStep['step_number']);
  const prefix = Number.isFinite(stepNumber) ? `Step ${stepNumber}` : 'Step';
  return `${prefix}: ${singleLine(typeof actionName === 'string' ? actionName : 'Action failed')}`;
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

function safeRecordingUrl(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || /[\r\n]/.test(trimmed)) return null;

  try {
    const local = mediaUrl(trimmed);
    if (!local) return null;
    const url = new URL(local, window.location.origin);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return url.href.replace(/[()]/g, character => encodeURIComponent(character));
  } catch {
    return null;
  }
}

function singleLine(value: string): string {
  return value.replace(/[\r\n]+/g, ' ').trim();
}
