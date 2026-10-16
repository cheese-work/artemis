import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { resolveBuildSha } from './write-build-info.mjs';

const appDirectory = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const envSha = 'ABCDEF0123456789abcdef0123456789abcdef01';

test('the environment sha wins over git', () => {
  assert.equal(resolveBuildSha({ ARTEMIS_BUILD_SHA: envSha }, appDirectory), envSha.toLowerCase());
});

test('git HEAD is used when the environment sha is unset or malformed', () => {
  const head = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: appDirectory, encoding: 'utf8' }).trim();
  assert.equal(resolveBuildSha({}, appDirectory), head);
  assert.equal(resolveBuildSha({ ARTEMIS_BUILD_SHA: 'not-a-sha' }, appDirectory), head);
});

test('no git and no environment sha reads as unknown', () => {
  const outside = mkdtempSync(path.join(tmpdir(), 'build-info-'));
  try {
    assert.equal(resolveBuildSha({}, outside), 'unknown');
  } finally {
    rmSync(outside, { recursive: true, force: true });
  }
});
