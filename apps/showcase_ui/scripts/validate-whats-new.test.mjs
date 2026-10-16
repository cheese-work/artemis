import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const script = fileURLToPath(new URL('./validate-whats-new.mjs', import.meta.url));

function combine(files) {
  const root = mkdtempSync(path.join(tmpdir(), 'whats-new-'));
  const entries = path.join(root, 'entries');
  const out = path.join(root, 'whats-new.json');
  mkdirSync(entries);
  for (const [name, content] of Object.entries(files)) {
    writeFileSync(path.join(entries, name), typeof content === 'string' ? content : JSON.stringify(content));
  }
  try {
    const result = spawnSync(process.execPath, [script, entries, out], { encoding: 'utf8' });
    return result.status === 0
      ? { ok: true, list: JSON.parse(readFileSync(out, 'utf8')) }
      : { ok: false, error: result.stderr };
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

const entry = (id, extra = {}) => ({ id, date: id.slice(0, 10), title: `Title ${id}`, ...extra });

test('lists entries newest first', () => {
  const result = combine({
    '2026-10-01-old.json': entry('2026-10-01-old'),
    '2026-10-08-new.json': entry('2026-10-08-new', { body: 'Body', issues: ['CHE-1334'] }),
  });
  assert.deepEqual(result.list.map(item => item.id), ['2026-10-08-new', '2026-10-01-old']);
  assert.deepEqual(result.list[0], entry('2026-10-08-new', { body: 'Body', issues: ['CHE-1334'] }));
});

test('orders entries with the same date by id', () => {
  const result = combine({
    '2026-10-08-zeta.json': entry('2026-10-08-zeta'),
    '2026-10-08-alpha.json': entry('2026-10-08-alpha'),
  });
  assert.deepEqual(result.list.map(item => item.id), ['2026-10-08-alpha', '2026-10-08-zeta']);
});

test('writes an empty list when there are no entry files', () => {
  assert.deepEqual(combine({}).list, []);
});

const rejected = {
  'an id that does not match the file name (duplicate id)': {
    '2026-10-08-a.json': entry('2026-10-08-a'),
    '2026-10-08-b.json': entry('2026-10-08-a'),
  },
  'a bad date': { '2026-02-30-bad.json': entry('2026-02-30-bad') },
  'a date that does not match the file name': { '2026-10-08-x.json': { ...entry('2026-10-08-x'), date: '2026-10-07' } },
  'a bad issue number': { '2026-10-08-x.json': entry('2026-10-08-x', { issues: ['che-1'] }) },
  'issues that are not an array': { '2026-10-08-x.json': entry('2026-10-08-x', { issues: 'CHE-1' }) },
  'a missing title': { '2026-10-08-x.json': { id: '2026-10-08-x', date: '2026-10-08' } },
  'an unknown key': { '2026-10-08-x.json': entry('2026-10-08-x', { link: 'https://example.com' }) },
  'invalid JSON': { '2026-10-08-x.json': '{"id": ' },
};
for (const [name, files] of Object.entries(rejected)) {
  test(`fails the build for ${name}`, () => {
    const result = combine(files);
    assert.equal(result.ok, false);
    assert.match(result.error, /What's New entry 2026-/);
  });
}
