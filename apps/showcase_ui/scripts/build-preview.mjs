import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const [number, sha] = process.argv.slice(2);
if (!/^[1-9]\d*$/.test(number || '') || !/^[a-f\d]{40}$/i.test(sha || '') || process.argv.length !== 4) {
  console.error('Usage: npm run build:preview -- <PR number> <full source SHA>');
  process.exit(2);
}
const result = spawnSync('npm', ['run', 'build', '--', '--base-href', `/preview/pr/${number}/`], {
  stdio: 'inherit',
  env: { ...process.env, ARTEMIS_BUILD_SHA: sha }
});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status || 1);
const index = 'dist/frontend/browser/index.html';
const html = readFileSync(index, 'utf8');
writeFileSync(index, html.replace('</head>', `<meta name="artemis-preview-sha" content="${sha.toLowerCase()}"></head>`));
