import { RunSummary } from '../core/models/run.model';

export function sessionRows(runs: RunSummary[]): RunSummary[] {
  const sessions = new Map<string, RunSummary>();
  for (const run of runs) {
    const identity = run.chat_session_id ?? run.session_id;
    if (!sessions.has(identity)) sessions.set(identity, run.chat_prompt ? { ...run, prompt: run.chat_prompt } : run);
  }
  return [...sessions.values()];
}
