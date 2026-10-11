import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const root = fileURLToPath(new URL('..', import.meta.url));
const names = readFileSync(path.join(root, 'public/fonts/material-symbols-outlined.txt'), 'utf8').trim().split(',');

test('icons use a small local WOFF2 with block display, not Google Fonts', () => {
  const styles = readFileSync(path.join(root, 'src/styles.scss'), 'utf8');
  assert.match(styles, /@font-face\s*\{[^}]*font-family: 'Material Symbols Outlined';[^}]*font-display: block;[^}]*url\('\.\.\/public\/fonts\/material-symbols-outlined\.woff2'\)/s);
  const index = readFileSync(path.join(root, 'src/index.html'), 'utf8');
  assert.doesNotMatch(index, /fonts\.googleapis\.com[^"']*Material[+ ]Symbols/);
  assert.match(index, /family=Inter/);
  const font = readFileSync(path.join(root, 'public/fonts/material-symbols-outlined.woff2'));
  assert.equal(font.subarray(0, 4).toString(), 'wOF2');
  assert.ok(font.length < 200 * 1024, `Icon subset is ${font.length} bytes`);
});

test('the sorted subset includes static icons and icon-valued properties', () => {
  assert.deepEqual(names, [...new Set(names)].sort());
  function visit(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (/\.(html|ts)$/.test(filename) && !filename.endsWith('.spec.ts')) {
        const source = readFileSync(filename, 'utf8');
        const patterns = [
          /<span\b[^>]*material-symbols-outlined[^>]*>\s*([a-z_0-9]+)\s*<\/span>/g,
          /\bicon\s*:\s*'([a-z_0-9]+)'/g
        ];
        for (const pattern of patterns) {
          for (const match of source.matchAll(pattern)) {
            assert.ok(names.includes(match[1]), `${filename}: missing ${match[1]}`);
          }
        }
      }
    }
  }
  visit(path.join(root, 'src'));
});
