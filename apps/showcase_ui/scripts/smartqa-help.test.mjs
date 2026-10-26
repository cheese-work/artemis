import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, '../../..');
const uiRoot = path.join(repositoryRoot, 'apps/showcase_ui');
const angular = JSON.parse(readFileSync(path.join(uiRoot, 'angular.json'), 'utf8'));
const buildAssets = angular.projects.frontend.architect.build.options.assets;
const sourceDocument = path.join(repositoryRoot, 'docs/smartqa-verdicts.md');
const renderer = path.join(uiRoot, 'scripts/render-smartqa-verdicts.mjs');
const runChatSource = readFileSync(path.join(uiRoot, 'src/app/components/run-chat/run-chat.component.ts'), 'utf8');
const helpRoute = JSON.parse(readFileSync(path.join(uiRoot, 'src/app/core/smartqa-help-route.json'), 'utf8'));

test('build serves the canonical verdict guide with category anchors', () => {
  assert.ok(buildAssets.some((asset) => typeof asset === 'object' && asset.input === 'public'),
    'Angular build must copy public assets');
  const guide = readFileSync(sourceDocument, 'utf8');
  const categories = [
    'smartqa_infra', 'smartqa_agent', 'provider', 'app_defect', 'user_prompt', 'qa_environment', 'unknown'
  ];
  const outputDirectory = mkdtempSync(path.join(os.tmpdir(), 'smartqa-help-'));
  const output = path.join(outputDirectory, helpRoute.path.replace(/^\//, ''));
  try {
    execFileSync(process.execPath, [renderer, '--output', output], { cwd: repositoryRoot });
    const page = readFileSync(output, 'utf8');
    for (const category of categories) {
      assert.match(guide, new RegExp(`^### ${category}$`, 'm'), `guide must have a ${category} heading`);
      assert.match(page, new RegExp(`id="${category}"`), `served guide must have a ${category} anchor`);
    }
  } finally {
    rmSync(outputDirectory, { recursive: true, force: true });
  }

  assert.match(runChatSource, /return `\$\{smartqaHelpRoute\.path\}#/,
    'category-specific verdict help must link to the generated guide');
});
