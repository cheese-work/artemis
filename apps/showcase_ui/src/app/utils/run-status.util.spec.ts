import { runStatusView } from './run-status.util';

describe('runStatusView', () => {
  const table: Array<[string | null | undefined, string, string, boolean]> = [
    // [stored status, key, label, active]
    ['pending', 'pending', 'Queued', true],
    ['running', 'running', 'Running', true],
    ['paused', 'paused', 'Paused', true],
    ['completed', 'completed', 'Passed', false],
    ['success', 'completed', 'Passed', false],
    ['failed', 'failed', 'Failed', false],
    ['interrupted', 'interrupted', 'Interrupted', false],
    ['cancelled', 'cancelled', 'Cancelled', false],
    ['INTERRUPTED', 'interrupted', 'Interrupted', false],
    ['something_new', 'unknown', 'Unknown', false],
    ['', 'unknown', 'Unknown', false],
    [null, 'unknown', 'Unknown', false],
    [undefined, 'unknown', 'Unknown', false]
  ];

  for (const [status, key, label, active] of table) {
    it(`maps ${JSON.stringify(status)} to ${key}`, () => {
      const view = runStatusView(status);
      expect(view.key).toBe(key);
      expect(view.label).toBe(label);
      expect(view.active).toBe(active);
    });
  }

  it('never shows interrupted, failed, cancelled or an unknown value as completed', () => {
    const completed = runStatusView('completed');
    for (const status of ['interrupted', 'failed', 'cancelled', 'something_new', null]) {
      const view = runStatusView(status);
      expect(view.key).not.toBe('completed');
      expect(view.label).not.toBe(completed.label);
    }
  });
});
