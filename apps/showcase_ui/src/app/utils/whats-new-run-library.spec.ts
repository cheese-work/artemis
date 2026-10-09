import { parseWhatsNewEntries } from './whats-new.util';

describe("What's New entry for the run library", () => {
  it('ships and describes search, replay, sharing, and the privacy warning', async () => {
    const entries = parseWhatsNewEntries(await (await fetch('/whats-new.json')).json());
    expect(entries.length).toBeGreaterThan(1);
    const entry = entries.find(item => item.id === '2026-10-05-smartqa-run-library')!;
    expect(entry).toBeDefined();
    expect(entry.title).toContain('run library');
    expect(entry.body).toContain('Runs');
    expect(entry.body).toMatch(/search/i);
    expect(entry.body).toMatch(/not redacted/i);
    expect(entry.body).not.toMatch(/daemon|host agent/i);
  });
});
