export type StepIdentityMatch = 'same' | 'conflict' | 'unrelated';

export function compareStepIdentity(leftValue: unknown, rightValue: unknown): StepIdentityMatch {
  const left = asRecord(leftValue);
  const right = asRecord(rightValue);
  if (!left || !right) return 'unrelated';

  const leftId = readIdentity(left['step_id']);
  const rightId = readIdentity(right['step_id']);
  if (leftId && rightId) return leftId === rightId ? 'same' : 'conflict';

  const rightTraceIds = new Set(getStepTraceIds(right));
  return getStepTraceIds(left).some(traceId => rightTraceIds.has(traceId)) ? 'same' : 'unrelated';
}

export function getStepTraceIds(value: unknown): string[] {
  const step = asRecord(value);
  if (!step || !Array.isArray(step['generic_tools'])) return [];

  return [...new Set(step['generic_tools']
    .map(tool => asRecord(tool))
    .map(tool => readIdentity(tool?.['trace_id']))
    .filter(Boolean))];
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function readIdentity(value: unknown): string {
  return value === undefined || value === null ? '' : String(value).trim();
}
