import { taskStatusOf } from './task-status.util';

describe('taskStatusOf', () => {
  const idle: { sessionId: string | null; status: string } = { sessionId: null, status: 'idle' };
  const of = (status: string | undefined, runner = idle) => taskStatusOf({ session_id: 's1', status }, runner);

  it('names interrupted as its own status', () => {
    expect(of('interrupted')).toBe('interrupted');
    expect(of('Interrupted')).toBe('interrupted');
  });

  it('never shows a status it does not know as completed', () => {
    expect(of('something_new')).toBe('unknown');
  });

  it('keeps the existing names', () => {
    expect(of('success')).toBe('completed');
    for (const status of ['completed', 'failed', 'cancelled', 'running', 'paused', 'pending']) {
      expect(of(status)).toBe(status as never);
    }
  });

  it('takes a status-less session from the runner while it runs, else completed', () => {
    expect(of(undefined, { sessionId: 's1', status: 'running' })).toBe('running');
    expect(of(undefined, { sessionId: 's1', status: 'paused' })).toBe('paused');
    expect(of(undefined, { sessionId: 's2', status: 'running' })).toBe('completed');
    expect(of(undefined)).toBe('completed');
  });
});
