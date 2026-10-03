import { Session } from '../core/models/session.model';
import { isActionFailed } from './action-formatter.util';
import { consolidateLogsToBlocks } from './stream-aggregator.util';
import { compareStepIdentity, getStepTraceIds } from './step-identity.util';

interface SummaryStepEntry {
  step: Record<string, unknown>;
  index: number;
}

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
  const latestSteps: SummaryStepEntry[] = [];
  const stepsById = new Map<string, SummaryStepEntry>();
  const stepsByNumber = new Map<number, SummaryStepEntry>();
  const stepsByTraceId = new Map<string, SummaryStepEntry>();
  const ambiguousStepNumbers = new Set<number>();
  const eventIndices = getSummaryEventIndices(logs);
  logs.forEach((log, index) => {
    if (!log || typeof log !== 'object') return;
    const event = log as Record<string, unknown>;
    const data = event['data'];
    if (['step_updated', 'step_recorded', 'step'].includes(String(event['type']))
      && data && typeof data === 'object') {
      const step = data as Record<string, unknown>;
      mergeStep(step, index, latestSteps, stepsById, stepsByNumber, stepsByTraceId, ambiguousStepNumbers);
      return;
    }
    if (event['step_number'] !== undefined || event['step_id'] !== undefined) {
      mergeStep(event, index, latestSteps, stepsById, stepsByNumber, stepsByTraceId, ambiguousStepNumbers);
    }
  });

  const streamEvents = logs.filter(log => {
    if (!log || typeof log !== 'object' || Array.isArray(log)) return false;
    const data = (log as Record<string, unknown>)['data'];
    return !!data && typeof data === 'object' && !Array.isArray(data);
  });
  for (const block of consolidateLogsToBlocks(streamEvents as any[])) {
    if (block.type !== 'step' || !block.data || typeof block.data !== 'object') continue;
    const step = block.data as Record<string, unknown>;
    const index = getSummaryStepEventIndex(step, eventIndices);
    if (index >= 0) {
      mergeStep(step, index, latestSteps, stepsById, stepsByNumber, stepsByTraceId, ambiguousStepNumbers);
    }
  }

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
  latestSteps: SummaryStepEntry[],
  stepsById: Map<string, SummaryStepEntry>,
  stepsByNumber: Map<number, SummaryStepEntry>,
  stepsByTraceId: Map<string, SummaryStepEntry>,
  ambiguousStepNumbers: Set<number>
): void {
  const stepId = step['step_id'] === undefined || step['step_id'] === null
    ? ''
    : String(step['step_id']).trim();
  const rawNumber = step['step_number'];
  const stepNumber = rawNumber === undefined || rawNumber === null || String(rawNumber).trim() === ''
    ? null
    : Number(rawNumber);
  const validStepNumber = stepNumber !== null && Number.isFinite(stepNumber) ? stepNumber : null;
  const existingById = stepId ? stepsById.get(stepId) : undefined;
  const existingByTrace = getStepTraceIds(step)
    .map(traceId => stepsByTraceId.get(traceId))
    .find((entry): entry is SummaryStepEntry =>
      !!entry && compareStepIdentity(entry.step, step) === 'same');
  let existingByNumber = validStepNumber !== null && !ambiguousStepNumbers.has(validStepNumber)
    ? stepsByNumber.get(validStepNumber)
    : undefined;

  if (stepId && existingByNumber) {
    const numberStepId = getStepId(existingByNumber.step);
    if (numberStepId && numberStepId !== stepId) {
      ambiguousStepNumbers.add(validStepNumber!);
      stepsByNumber.delete(validStepNumber!);
      existingByNumber = undefined;
    } else if (existingById && existingById !== existingByNumber) {
      coalesceStepEntries(existingById, existingByNumber, latestSteps, stepsById, stepsByNumber, stepsByTraceId);
      existingByNumber = existingById;
    }
  }

  const candidates = [existingById, existingByTrace, existingByNumber]
    .filter((entry, index, entries): entry is SummaryStepEntry => !!entry && entries.indexOf(entry) === index);
  const existing = candidates.shift();
  if (existing) {
    for (const alias of candidates) {
      if (compareStepIdentity(existing.step, alias.step) === 'conflict') continue;
      coalesceStepEntries(existing, alias, latestSteps, stepsById, stepsByNumber, stepsByTraceId);
    }
  }

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
  for (const traceId of getStepTraceIds(target.step)) stepsByTraceId.set(traceId, target);
  if (validStepNumber !== null && !ambiguousStepNumbers.has(validStepNumber)) {
    const numberEntry = stepsByNumber.get(validStepNumber);
    const numberStepId = numberEntry ? getStepId(numberEntry.step) : '';
    if (numberEntry && numberEntry !== target && numberStepId && stepId && numberStepId !== stepId) {
      ambiguousStepNumbers.add(validStepNumber);
      stepsByNumber.delete(validStepNumber);
    } else {
      stepsByNumber.set(validStepNumber, target);
    }
  }
}

function coalesceStepEntries(
  target: SummaryStepEntry,
  alias: SummaryStepEntry,
  latestSteps: SummaryStepEntry[],
  stepsById: Map<string, SummaryStepEntry>,
  stepsByNumber: Map<number, SummaryStepEntry>,
  stepsByTraceId: Map<string, SummaryStepEntry>
): void {
  const [older, newer] = target.index <= alias.index ? [target, alias] : [alias, target];
  target.step = {
    ...older.step,
    ...newer.step,
    generic_tools: mergeGenericTools(older.step['generic_tools'], newer.step['generic_tools'])
  };
  target.index = Math.max(target.index, alias.index);
  const aliasIndex = latestSteps.indexOf(alias);
  if (aliasIndex !== -1) latestSteps.splice(aliasIndex, 1);
  for (const [stepId, entry] of stepsById) {
    if (entry === alias) stepsById.set(stepId, target);
  }
  for (const [stepNumber, entry] of stepsByNumber) {
    if (entry === alias) stepsByNumber.set(stepNumber, target);
  }
  for (const [traceId, entry] of stepsByTraceId) {
    if (entry === alias) stepsByTraceId.set(traceId, target);
  }
}

function getStepId(step: Record<string, unknown>): string {
  return step['step_id'] === undefined || step['step_id'] === null
    ? ''
    : String(step['step_id']).trim();
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

interface SummaryEventIndices {
  stepIds: Map<string, number>;
  stepNumbers: Map<number, number>;
  traceIds: Map<string, number>;
}

function getSummaryEventIndices(logs: unknown[]): SummaryEventIndices {
  const indices: SummaryEventIndices = {
    stepIds: new Map(),
    stepNumbers: new Map(),
    traceIds: new Map()
  };
  logs.forEach((log, index) => {
    if (!log || typeof log !== 'object' || Array.isArray(log)) return;
    const event = log as Record<string, unknown>;
    const data = event['data'] && typeof event['data'] === 'object'
      ? event['data'] as Record<string, unknown>
      : event;
    const type = String(event['type'] ?? '');
    if (['step_updated', 'step_recorded', 'step'].includes(type)
      || data['step_id'] !== undefined || data['step_number'] !== undefined) {
      const stepId = data['step_id'] === undefined || data['step_id'] === null
        ? ''
        : String(data['step_id']).trim();
      const stepNumber = parseStepNumber(data['step_number']);
      if (stepId) indices.stepIds.set(stepId, index);
      if (stepNumber !== null) indices.stepNumbers.set(stepNumber, index);
    }
    if (type === 'trace_recorded') {
      const traceId = data['trace_id'] === undefined || data['trace_id'] === null
        ? ''
        : String(data['trace_id']).trim();
      const stepId = data['step_id'] === undefined || data['step_id'] === null
        ? ''
        : String(data['step_id']).trim();
      const stepNumber = parseStepNumber(data['step_number']);
      if (traceId) indices.traceIds.set(traceId, index);
      if (stepId) indices.stepIds.set(stepId, index);
      if (stepNumber !== null) indices.stepNumbers.set(stepNumber, index);
    }
  });
  return indices;
}

function getSummaryStepEventIndex(step: Record<string, unknown>, indices: SummaryEventIndices): number {
  const stepId = step['step_id'] === undefined || step['step_id'] === null
    ? ''
    : String(step['step_id']).trim();
  const stepNumber = parseStepNumber(step['step_number']);
  let latestIndex = Math.max(
    stepId ? indices.stepIds.get(stepId) ?? -1 : -1,
    stepNumber !== null ? indices.stepNumbers.get(stepNumber) ?? -1 : -1
  );
  if (Array.isArray(step['generic_tools'])) {
    for (const tool of step['generic_tools']) {
      if (!tool || typeof tool !== 'object') continue;
      const traceId = String((tool as Record<string, unknown>)['trace_id'] ?? '').trim();
      if (traceId) latestIndex = Math.max(latestIndex, indices.traceIds.get(traceId) ?? -1);
    }
  }
  return latestIndex;
}

function parseStepNumber(value: unknown): number | null {
  if (value === undefined || value === null || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
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
