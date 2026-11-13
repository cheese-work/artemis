import assert from 'node:assert/strict';
import { test } from 'node:test';
import { checkWhatsNewEntry } from './check-whats-new-entry.mjs';

const UI = 'M\tapps/showcase_ui/src/app/pages/workspace/workspace.component.ts';
const ENTRY = 'A\tapps/showcase_ui/whats-new/entries/2026-10-08-new-thing.json';

test('passes a UI change that adds an entry', () => {
  assert.deepEqual(checkWhatsNewEntry([UI, ENTRY], []), { ok: true });
});

test('fails a UI change without an entry and names the folder, format and label', () => {
  const result = checkWhatsNewEntry([UI, ''], []);
  assert.equal(result.ok, false);
  assert.match(result.message, /apps\/showcase_ui\/whats-new\/entries\/<YYYY-MM-DD>-<slug>\.json/);
  assert.match(result.message, /"issues": \["CHE-1234"\]/);
  assert.match(result.message, /no-whats-new/);
});

test('passes a UI change with the no-whats-new label', () => {
  assert.deepEqual(checkWhatsNewEntry([UI], ['bug', 'no-whats-new']), { ok: true });
});

test('passes a change to spec files only', () => {
  assert.deepEqual(checkWhatsNewEntry(['M\tapps/showcase_ui/src/app/utils/whats-new.util.spec.ts'], []), { ok: true });
});

test('passes a change outside the UI', () => {
  assert.deepEqual(checkWhatsNewEntry(['M\tartemis/runtime/foo.py', 'M\tapps/showcase_ui/scripts/x.mjs'], []), { ok: true });
});

test('does not count an edited entry as a new one', () => {
  const edited = 'M\tapps/showcase_ui/whats-new/entries/2026-10-05-smartqa-run-library.json';
  assert.equal(checkWhatsNewEntry([UI, edited], []).ok, false);
});
