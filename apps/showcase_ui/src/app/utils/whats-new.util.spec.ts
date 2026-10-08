import whatsNewData from '../../../public/whats-new.json';
import { parseWhatsNewEntries, shouldAutoOpenWhatsNew } from './whats-new.util';

describe('parseWhatsNewEntries', () => {
  it('accepts the shipped update data with schema and newest-first ordering', () => {
    expect(parseWhatsNewEntries(whatsNewData)).toEqual(whatsNewData);
  });

  it('rejects malformed entries instead of silently keeping a partial list', () => {
    expect(parseWhatsNewEntries([
      { id: 'valid', date: '2026-10-03', title: 'Update' },
      { id: 'invalid', date: '2026-02-30', title: 'Bad date' }
    ])).toEqual([]);
  });

  it('rejects entries that are not newest-first or have duplicate ids', () => {
    expect(parseWhatsNewEntries([
      { id: 'old', date: '2026-10-03', title: 'Old' },
      { id: 'new', date: '2026-10-04', title: 'New' }
    ])).toEqual([]);
    expect(parseWhatsNewEntries([
      { id: 'same', date: '2026-10-03', title: 'First' },
      { id: 'same', date: '2026-10-02', title: 'Second' }
    ])).toEqual([]);
  });
});

describe('shouldAutoOpenWhatsNew', () => {
  const entries = [
    { id: 'second', date: '2026-10-03', title: 'Second update' },
    { id: 'first', date: '2026-10-02', title: 'First update' }
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

  it('opens when a new newest entry is added before the previously seen entry', () => {
    const newestFirst = [
      { id: 'third', date: '2026-10-04', title: 'Third update' },
      ...entries.slice().reverse()
    ];
    expect(shouldAutoOpenWhatsNew(newestFirst, 'second', false)).toBeTrue();
    expect(shouldAutoOpenWhatsNew(newestFirst, 'third', false)).toBeFalse();
  });

  it('does not open automatically during an active run', () => {
    expect(shouldAutoOpenWhatsNew(entries, null, true)).toBeFalse();
  });

  it('does not open automatically while a prompt draft or error is visible', () => {
    expect(shouldAutoOpenWhatsNew(entries, null, false, true)).toBeFalse();
    expect(shouldAutoOpenWhatsNew(entries, null, false, false, true)).toBeFalse();
  });

  it('treats an unknown last-seen id as unseen content', () => {
    expect(shouldAutoOpenWhatsNew(entries, 'retired-entry', false)).toBeTrue();
  });
});
