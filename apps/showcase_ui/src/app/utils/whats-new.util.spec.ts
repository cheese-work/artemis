import { shouldAutoOpenWhatsNew } from './whats-new.util';

describe('shouldAutoOpenWhatsNew', () => {
  const entries = [
    { id: 'first', date: '2026-10-02', title: 'First update' },
    { id: 'second', date: '2026-10-03', title: 'Second update' }
  ];

  it('opens for a first-time visitor when entries exist', () => {
    expect(shouldAutoOpenWhatsNew(entries, null, false)).toBeTrue();
  });

  it('stays closed when the latest entry was already seen', () => {
    expect(shouldAutoOpenWhatsNew(entries, 'second', false)).toBeFalse();
  });

  it('opens when an entry is newer than the last-seen id', () => {
    expect(shouldAutoOpenWhatsNew(entries, 'first', false)).toBeTrue();
  });

  it('does not open automatically during an active run', () => {
    expect(shouldAutoOpenWhatsNew(entries, null, true)).toBeFalse();
  });

  it('treats an unknown last-seen id as unseen content', () => {
    expect(shouldAutoOpenWhatsNew(entries, 'retired-entry', false)).toBeTrue();
  });
});
