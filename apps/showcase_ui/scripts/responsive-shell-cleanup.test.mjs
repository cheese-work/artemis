import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { cleanupResponsiveShell } from './responsive-shell-cleanup.mjs';

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const smokeScript = fileURLToPath(new URL('./responsive-shell.mjs', import.meta.url));
const groupExists = pid => {
  if (!pid) return false;
  try {
    process.kill(-pid, 0);
    return true;
  } catch (error) {
    if (error.code === 'ESRCH') return false;
    throw error;
  }
};

const runSmoke = args => new Promise((resolve, reject) => {
  const child = spawn(process.execPath, [smokeScript, ...args], { detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  const startedAt = Date.now();
  let stdout = '';
  let stderr = '';
  let chromeGroup;
  const watchdog = setTimeout(() => {
    if (child.pid && groupExists(child.pid)) process.kill(-child.pid, 'SIGKILL');
    if (chromeGroup && groupExists(chromeGroup)) process.kill(-chromeGroup, 'SIGKILL');
  }, 120_000);
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', chunk => {
    stdout += chunk;
    chromeGroup ??= Number(stdout.match(/CHROME_GROUP=(\d+)/)?.[1]);
  });
  child.stderr.on('data', chunk => { stderr += chunk; });
  child.once('error', error => {
    clearTimeout(watchdog);
    reject(error);
  });
  child.once('close', (code, signal) => {
    clearTimeout(watchdog);
    resolve({ code, signal, durationMs: Date.now() - startedAt, stdout, stderr, childPid: child.pid, chromeGroup });
  });
});

const assertSmokeAssertionsPassed = output => {
  for (const width of [1440, 1200, 1199, 1024, 1023, 800, 799, 390, 320]) {
    assert.match(output, new RegExp(`PASS ${width}px:`), `${width}px responsive assertions must run`);
  }
};

const assertProcessGone = (pid, label) => {
  if (!pid) return;
  let exists = false;
  try {
    process.kill(pid, 0);
    exists = true;
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
  assert.equal(exists, false, `${label} process must exit`);
};

test('bounded cleanup terminates the browser group and closes its server when Browser.close stalls', async () => {
  const chrome = spawn(process.execPath, [
    '-e',
    "const { spawn } = require('node:child_process'); spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' }); setInterval(() => {}, 1000);"
  ], { detached: true, stdio: 'ignore' });
  const chromeExit = once(chrome, 'exit');
  const server = createServer((_request, response) => response.end('ok'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const profile = mkdtempSync(path.join(tmpdir(), 'responsive-shell-cleanup-'));
  const startedAt = Date.now();
  let cleanup;
  let timedOut = false;

  try {
    cleanup = await Promise.race([
      cleanupResponsiveShell({
        chrome,
        sendBrowserClose: () => new Promise(() => {}),
        chromeExit,
        server,
        profile,
        timeoutMs: 100,
        processGraceMs: 100
      }),
      delay(500).then(() => {
        timedOut = true;
      })
    ]);
  } finally {
    if (groupExists(chrome.pid)) process.kill(-chrome.pid, 'SIGKILL');
    if (server.listening) await new Promise(resolve => server.close(resolve));
    if (existsSync(profile)) rmSync(profile, { recursive: true, force: true });
    await Promise.race([chromeExit, delay(1000)]);
  }

  assert.equal(timedOut, false, 'teardown must finish inside its bound');
  assert.ok(Date.now() - startedAt < 500, 'teardown duration must be bounded');
  assert.equal(groupExists(chrome.pid), false, 'browser process group must be absent');
  assert.equal(server.listening, false, 'mock server must be closed');
  assert.equal(existsSync(profile), false, 'browser profile must be removed');
  assert.deepEqual(cleanup.errors.map(error => error.message), ['Browser.close timed out after 100 ms']);
  assert.equal(cleanup.forcedTermination, true);
});

test('responsive assertions pass and normal plus stalled teardown leave no child processes', async () => {
  const normal = await runSmoke([]);
  const normalOutput = normal.stdout + normal.stderr;
  assert.equal(normal.code, 0, normalOutput);
  assertSmokeAssertionsPassed(normal.stdout);
  assert.match(normal.stdout, /CLEANUP duration=\d+ms chrome-group=\d+ forced=(?:true|false)/);
  assert.doesNotMatch(normal.stderr, /CLEANUP ERROR:/);
  assertProcessGone(normal.childPid, 'responsive-shell Node/server');
  assert.equal(groupExists(normal.chromeGroup), false, 'normal Chrome process group must be absent');
  const normalCleanupDuration = Number(normal.stdout.match(/CLEANUP duration=(\d+)ms/)?.[1]);
  assert.ok(Number.isFinite(normalCleanupDuration));
  console.log(`NORMAL exit=${normal.code} total=${normal.durationMs}ms cleanup=${normalCleanupDuration}ms Chrome=absent Node/server=absent`);

  const stalled = await runSmoke(['--test-stall-browser-close']);
  const stalledOutput = stalled.stdout + stalled.stderr;
  assert.equal(stalled.code, 1, stalledOutput);
  assertSmokeAssertionsPassed(stalled.stdout);
  assert.match(stalled.stdout, /CLEANUP duration=\d+ms chrome-group=\d+ forced=true/);
  assert.match(stalled.stderr, /CLEANUP ERROR: Browser\.close timed out after 2000 ms/);
  assertProcessGone(stalled.childPid, 'stalled responsive-shell Node/server');
  assert.equal(groupExists(stalled.chromeGroup), false, 'stalled Chrome process group must be absent');
  const cleanupDuration = Number(stalled.stdout.match(/CLEANUP duration=(\d+)ms/)?.[1]);
  assert.ok(cleanupDuration <= 7500, `stalled teardown took ${cleanupDuration} ms`);
  console.log(`STALLED exit=${stalled.code} total=${stalled.durationMs}ms cleanup=${cleanupDuration}ms Chrome=absent Node/server=absent`);
});

const spawnLeakyChrome = () => spawn(process.execPath, [
  '-e',
  "const { spawn } = require('node:child_process'); spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' }); setInterval(() => {}, 1000);"
], { detached: true, stdio: 'ignore' });

test('cleanup tolerates a slow Chrome exit after Browser.close on a loaded runner', async () => {
  const chrome = spawnLeakyChrome();
  const chromeExit = once(chrome, 'exit');
  // Browser.close is acknowledged at once; Chrome itself needs 400 ms (> timeoutMs) to wind down.
  const slowExit = delay(400).then(() => process.kill(-chrome.pid, 'SIGKILL'));
  try {
    const cleanup = await cleanupResponsiveShell({
      chrome,
      sendBrowserClose: async () => {},
      chromeExit,
      timeoutMs: 100,
      chromeExitTimeoutMs: 2000,
      processGraceMs: 100
    });
    assert.deepEqual(cleanup.errors, []);
  } finally {
    await slowExit;
    if (groupExists(chrome.pid)) process.kill(-chrome.pid, 'SIGKILL');
  }
  assert.equal(groupExists(chrome.pid), false, 'browser process group must be absent');
});

test('cleanup still reports and kills a Chrome that never exits after Browser.close', async () => {
  const chrome = spawnLeakyChrome();
  const chromeExit = once(chrome, 'exit');
  try {
    const cleanup = await cleanupResponsiveShell({
      chrome,
      sendBrowserClose: async () => {},
      chromeExit: new Promise(() => {}),
      timeoutMs: 100,
      chromeExitTimeoutMs: 200,
      processGraceMs: 100
    });
    assert.deepEqual(cleanup.errors.map(error => error.message), ['Chrome exit timed out after 200 ms']);
    assert.equal(cleanup.forcedTermination, true);
  } finally {
    if (groupExists(chrome.pid)) process.kill(-chrome.pid, 'SIGKILL');
    await Promise.race([chromeExit, delay(1000)]);
  }
  assert.equal(groupExists(chrome.pid), false, 'leaked browser process group must be killed');
});
