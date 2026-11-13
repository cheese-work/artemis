import { runStatusView, sessionStatusView } from './run-status.util';

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
    [undefined, 'unknown', 'Unknown', false],
    // Names inherited from Object.prototype are not statuses (CHE-1261 R2).
    ['toString', 'unknown', 'Unknown', false],
    ['constructor', 'unknown', 'Unknown', false],
    ['__proto__', 'unknown', 'Unknown', false],
    ['hasOwnProperty', 'unknown', 'Unknown', false]
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

  it('gives every inherited-name status a full view, never a blank badge (R2)', () => {
    for (const status of ['toString', 'constructor', '__proto__']) {
      const view = runStatusView(status);
      expect(view.label).toBe('Unknown');
      expect(view.icon).toBe('help');
      expect(view.tone).toBe('neutral');
      expect(view.active).toBe(false);
    }
  });
});

describe('sessionStatusView', () => {
  it('uses the live status only when the stored status is missing', () => {
    expect(sessionStatusView(undefined, 'running').key).toBe('running');
    expect(sessionStatusView(null, 'paused').key).toBe('paused');
    expect(sessionStatusView('', 'running').key).toBe('running');
  });

  it('shows an explicit unrecognised stored status as Unknown even while live (R1)', () => {
    expect(sessionStatusView('something_new', 'running').key).toBe('unknown');
    expect(sessionStatusView('something_new', 'paused').key).toBe('unknown');
    expect(sessionStatusView('toString', 'running').label).toBe('Unknown');
  });

  it('keeps a recognised stored status over the live one', () => {
    expect(sessionStatusView('interrupted', 'running').key).toBe('interrupted');
  });

  it('shows a missing status with no live run as Unknown', () => {
    expect(sessionStatusView(undefined, null).key).toBe('unknown');
    expect(sessionStatusView(undefined, 'idle').key).toBe('unknown');
  });
});
