export type FailureCategory = 'smartqa_infra' | 'smartqa_agent' | 'provider' | 'user_prompt' | 'unknown';

export interface FailureCount {
  day: string;
  category: FailureCategory;
  count: number;
}

export interface FailureCause {
  category: FailureCategory;
  rule: string;
  count: number;
  sample: string;
  session_ids: string[];
  action: 'fix' | 'monitor' | 'review' | 'no action';
}

export interface FailureReport {
  days: number;
  counts: FailureCount[];
  causes: FailureCause[];
}
