// Keyboard-only walkthrough of the run library and viewer, run in real headless Chrome.
// Every interaction is a trusted key press sent through the DevTools protocol
// (Tab, Shift+Tab, Enter, Escape, typing); the page is never clicked or scripted.
//
//   npm run build && npm run test:keyboard      (CHROME_BIN overrides the browser)
//   npm run test:keyboard -- --record=<dir>     also writes <dir>/keyboard-walkthrough.mp4 (a screen recording with a key caption)
//
// Prints a transcript of what had focus after each key and exits 1 on the first miss.
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, existsSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock, RUNS } from './mock-api.mjs';
import { pasteShortcut } from './paste-shortcut.mjs';

const dist = fileURLToPath(new URL('../../dist/frontend/browser', import.meta.url));
if (!existsSync(path.join(dist, 'index.html'))) {
  console.error('No build found. Run `npm run build` first.');
  process.exit(2);
}

const freePort = () =>
  new Promise((resolve) => {
    const s = createServer().listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const navigationOnly = process.argv.includes('--navigation-only');
const recordDir = process.argv.find((a) => a.startsWith('--record='))?.slice('--record='.length);

mock.readiness = {
  os_type: 'linux', overall_ready: true,
  probes: [
    { id: 'system_config', status: 'pass' },
    { id: 'llm_api_key', status: 'pass', metadata: { is_set: true } },
    { id: 'android_adb', status: 'pass', metadata: { devices: navigationOnly ? [] : [{ serial: 'keyboard-fixture', state: 'device', model: 'Pixel test', device_kind: 'phone' }] } }
  ]
};
const { server, url: base } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'kbd-walk-'));
const port = await freePort();
const chrome = spawn(
  process.env.CHROME_BIN || 'google-chrome',
  ['--headless=new', '--no-sandbox', '--password-store=basic', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1280,900', 'about:blank'],
  { stdio: 'ignore' }
);
chrome.on('error', (error) => {
  console.error(`Could not start Chrome (${error.message}). Set CHROME_BIN.`);
  process.exit(2);
});
// A hung browser must not hang the run: give the whole walkthrough two minutes.
setTimeout(() => {
  console.error('\nKeyboard walkthrough FAILED: timed out after 120 s');
  chrome.kill();
  server.close();
  process.exit(1);
}, 120_000).unref();

let ws;
let nextId = 1;
const pending = new Map();
const send = (method, params = {}) =>
  new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error('Chrome connection is closed'));
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
const evaluate = async (expression) => {
  const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (exceptionDetails) throw new Error(exceptionDetails.text + ' ' + (exceptionDetails.exception?.description ?? ''));
  return result.value;
};

let stopRecording = async () => {};
async function cleanup(code) {
  await stopRecording();
  if (chrome.exitCode === null && chrome.signalCode === null) {
    const stopped = new Promise((resolve) => chrome.once('exit', resolve));
    await send('Browser.close').catch(() => chrome.kill());
    await stopped;
  }
  try { ws?.close(); } catch { /* already closed */ }
  await new Promise((resolve) => server.close(resolve));
  await sleep(200);
  rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  process.exit(code);
}

const KEYS = {
  Tab: { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 },
  Space: { key: ' ', code: 'Space', windowsVirtualKeyCode: 32, text: ' ' },
  Enter: { key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' },
  Escape: { key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 },
  ArrowDown: { key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 },
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', windowsVirtualKeyCode: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39 },
  Home: { key: 'Home', code: 'Home', windowsVirtualKeyCode: 36 },
  End: { key: 'End', code: 'End', windowsVirtualKeyCode: 35 }
};
async function press(name, modifiers = 0) {
  const k = KEYS[name];
  await send('Input.dispatchKeyEvent', { type: k.text ? 'keyDown' : 'rawKeyDown', modifiers, ...k });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode });
  await sleep(120);
  await caption(`${modifiers & 8 ? 'Shift+' : ''}${name}`);
}
async function type(text) {
  for (const ch of text) {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ch, text: ch, unmodifiedText: ch });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch });
    await sleep(40);
  }
  await sleep(150);
}

// Recording only: a caption with the last key and what has focus, so the video reads without the transcript.
async function caption(keys) {
  if (!recordDir) return;
  const label = await describeFocus().catch(() => '');
  await evaluate(`(() => { let c = document.getElementById('kbd-caption');
    if (!c) { c = document.createElement('div'); c.id = 'kbd-caption';
      c.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:99999;padding:6px 10px;border-radius:6px;font:600 14px monospace;color:#fff;background:#0f172a;pointer-events:none';
      document.body.appendChild(c); }
    c.textContent = ${JSON.stringify(keys + '  ->  ' + label)}; })()`).catch(() => {});
}

const describeFocus = () =>
  evaluate(`(() => { const e = document.activeElement; if (!e) return 'none';
    const name = e.getAttribute('aria-label') || (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 50) || e.tagName;
    return name + ' <' + e.tagName.toLowerCase() + '>'; })()`);

const lines = [];
const log = (text) => {
  lines.push(text);
  console.log(text);
};
async function expectTrue(label, expression, timeout = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    // The page may be mid-navigation or the element not rendered yet: a throw here means "not yet".
    if (await evaluate(expression).catch(() => false)) return log(`  ok   ${label}`);
    await sleep(100);
  }
  log(`  FAIL ${label}  (${expression})`);
  throw new Error(label);
}
async function tabUntil(label, predicate, { back = false, max = 60 } = {}) {
  for (let i = 1; i <= max; i++) {
    await press('Tab', back ? 8 : 0);
    if (await evaluate(`(() => { const e = document.activeElement; return !!e && (${predicate}); })()`).catch(() => false)) {
      return log(`  ${back ? 'Shift+Tab' : 'Tab'} x${i} -> ${await describeFocus()}   [${label}]`);
    }
  }
  log(`  FAIL could not reach: ${label}`);
  throw new Error(label);
}
const focusIs = (selector) => `e.matches(${JSON.stringify(selector)})`;
const focusNamed = (name) => `(e.getAttribute('aria-label') || e.textContent || '').trim().replace(/\\s+/g, ' ').startsWith(${JSON.stringify(name)})`;

async function checkRunKeyboard(mode) {
  log(`${mode}: steps, share notice, download notice, technical details`);
  await expectTrue('four steps are loaded', `document.querySelectorAll('button.step-button').length === 4`);
  await tabUntil('first step', focusIs('button.step-button'));
  await press('Enter');
  await expectTrue('Enter selected the step', `document.activeElement.getAttribute('aria-current') === 'step'`);
  await press('ArrowDown');
  await expectTrue('ArrowDown selected the next step', `document.activeElement.textContent.includes('Step 2') && document.activeElement.getAttribute('aria-current') === 'step'`);
  await press('End');
  await expectTrue('End selected the last step', `document.activeElement.textContent.includes('Step 4')`);
  await press('Home');
  await press('Space');
  await expectTrue('Home and Space selected the first step', `document.activeElement.textContent.includes('Step 1') && document.activeElement.getAttribute('aria-current') === 'step'`);
  await tabUntil('Copy link', focusNamed('Copy link'));
  await press('Enter');
  await expectTrue('share dialog is open, focus is inside it', `!!document.querySelector('dialog.trust-dialog[open]') && document.querySelector('dialog.trust-dialog').contains(document.activeElement)`);
  await expectTrue('share dialog shows both notices', `document.querySelector('dialog.trust-dialog').textContent.includes('not redacted') && document.querySelector('dialog.trust-dialog').textContent.includes('Cloudflare Access')`);
  await press('Escape');
  await expectTrue('Escape returned focus to Copy link', `!document.querySelector('dialog.trust-dialog[open]') && ${`(() => { const e = document.activeElement; return ${focusNamed('Copy link')}; })()`}`);
  await tabUntil('Download', focusNamed('Download'));
  await press('Space');
  await expectTrue('download dialog shows only the redaction notice', `!!document.querySelector('dialog.trust-dialog[open]') && document.querySelector('dialog.trust-dialog').textContent.includes('not redacted') && !document.querySelector('dialog.trust-dialog').textContent.includes('Cloudflare Access')`);
  await press('Escape');
  await expectTrue('Escape returned focus to Download', `!document.querySelector('dialog.trust-dialog[open]') && ${`(() => { const e = document.activeElement; return ${focusNamed('Download')}; })()`}`);
  await tabUntil('Pin', focusNamed('Pin'));
  await tabUntil('Technical details', focusNamed('Technical details'));
  await press('Enter');
  await expectTrue('Technical details opened and raw logs rendered', `!!document.querySelector('pre.raw-logs')`);
}

try {
  for (let i = 0; i < 50; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/list`);
      const page = (await res.json()).find((t) => t.type === 'page');
      if (page) {
        ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
        break;
      }
    } catch { await sleep(100); }
  }
  if (!ws) throw new Error('could not reach Chrome');
  ws.onclose = () => {
    for (const { reject } of pending.values()) reject(new Error('Chrome connection closed'));
    pending.clear();
  };
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    }
  };
  await send('Page.enable');
  const frames = [];
  if (recordDir) {
    mkdirSync(recordDir, { recursive: true });
    const framesDir = path.join(recordDir, 'frames');
    mkdirSync(framesDir, { recursive: true });
    ws.addEventListener('message', (event) => {
      const msg = JSON.parse(event.data);
      if (msg.method !== 'Page.screencastFrame') return;
      const file = path.join(framesDir, `${String(frames.length).padStart(5, '0')}.jpg`);
      writeFileSync(file, Buffer.from(msg.params.data, 'base64'));
      frames.push({ file, ts: msg.params.metadata.timestamp });
      send('Page.screencastFrameAck', { sessionId: msg.params.sessionId }).catch(() => {});
    });
    await send('Page.startScreencast', { format: 'jpeg', quality: 80, everyNthFrame: 1 });
    stopRecording = async () => {
      await send('Page.stopScreencast').catch(() => {});
      if (frames.length < 2) return;
      const list = frames.map((f, i) => `file '${f.file}'\nduration ${Math.max(0.04, ((frames[i + 1]?.ts ?? f.ts + 1) - f.ts)).toFixed(3)}`).join('\n');
      writeFileSync(path.join(recordDir, 'frames.txt'), `${list}\nfile '${frames.at(-1).file}'\n`);
      await new Promise((resolve) => spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', path.join(recordDir, 'frames.txt'),
        '-vf', 'fps=15,scale=trunc(iw/2)*2:trunc(ih/2)*2', '-pix_fmt', 'yuv420p', path.join(recordDir, 'keyboard-walkthrough.mp4')], { stdio: 'inherit' }).on('exit', resolve));
      rmSync(framesDir, { recursive: true, force: true });
      rmSync(path.join(recordDir, 'frames.txt'), { force: true });
    };
  }
  if (navigationOnly) {
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `
      window.phoneChooserRequests = 0;
      Object.defineProperty(navigator, 'usb', { value: {
        getDevices: async () => [],
        requestDevice: async () => {
          window.phoneChooserRequests++;
          throw new DOMException('Cancelled fixture chooser', 'NotFoundError');
        },
        addEventListener: () => {}, removeEventListener: () => {}
      } });
    ` });
    mock.identity = { body: { email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null } };
    for (const route of ['/workspace', '/runs']) {
      log(`Navigation: non-admin on ${route}`);
      await send('Page.navigate', { url: `${base}${route}` });
      await expectTrue('user identity loaded', `!!document.querySelector('summary[aria-label^="User menu"]')`);
      await expectTrue('no Setup tab', `!document.querySelector('nav > a[href="/setup"]')`);
      await tabUntil('phone chip', focusIs('button.chip'));
      await press('Enter');
      await expectTrue('first activation opens the picker with Connect focused', `document.activeElement.textContent.includes('Connect a phone from this browser')`);
      await expectTrue('Connect is visible without scrolling', `document.activeElement.getBoundingClientRect().bottom <= innerHeight && scrollY === 0`);
      await press('Enter');
      await expectTrue('second activation reaches the mocked chooser, with no device access', `window.phoneChooserRequests === 1 && !document.querySelector('.chip-host .panel') && location.pathname === '${route}'`);
      await tabUntil('user menu', focusIs('summary[aria-label^="User menu"]'));
      await press('Enter');
      await expectTrue('Enter opens the QA user menu without Setup', `document.querySelector('details.identity-menu').open && !document.querySelector('app-admin-identity-indicator a[href="/setup"]')`);
      await press('Escape');
      await expectTrue('Escape closes the user menu and returns focus', `!document.querySelector('details.identity-menu').open && document.activeElement.matches('summary')`);
      await tabUntil('phone chip', focusIs('button.chip'), { back: true });
      await press('Space');
      await tabUntil('More options', focusIs('a.more'));
      await expectTrue('QAs can reach device settings through More options', `document.activeElement.getAttribute('href') === '/setup'`);
      await press('Escape');
      await expectTrue('Escape closes the phone picker with focus restored', `!document.querySelector('.chip-host .panel') && document.activeElement.matches('button.chip')`);
    }
    mock.identity.body.admin = true;
    await send('Page.navigate', { url: `${base}/workspace` });
    await expectTrue('admin identity loaded', `document.querySelector('.identity-role')?.textContent.includes('Admin')`);
    await tabUntil('admin user menu', focusIs('summary[aria-label^="User menu"]'));
    await press('Space');
    await expectTrue('Space opens the admin user menu', `document.querySelector('details.identity-menu').open`);
    await press('Tab');
    await expectTrue('Tab reaches Setup only inside the admin user menu', `document.activeElement.matches('app-admin-identity-indicator a[href="/setup"]') && !document.querySelector('nav > a[href="/setup"]')`);
    await press('Escape');
    await expectTrue('Escape restores focus to the admin menu trigger', `document.activeElement.matches('summary') && !document.querySelector('details.identity-menu').open`);
    log('\nNavigation keyboard walkthrough passed. WebUSB was mocked; no device was accessed.');
    await cleanup(0);
  }
  await send('Page.navigate', { url: `${base}/runs` });
  await expectTrue('library loaded', `document.querySelectorAll('a.run-row').length === 6`);

  log("Library: My runs and Everyone's runs tabs");
  await tabUntil('My runs tab', focusIs('[role="tab"][data-scope="mine"]'));
  await press('ArrowRight');
  await expectTrue("ArrowRight showed Everyone's runs: the other QA's run is listed with its owner", `document.activeElement.matches('[data-scope="everyone"]') && location.search.includes('scope=everyone') && document.querySelectorAll('a.run-row').length === 7 && document.body.textContent.includes('other@example.test')`);
  await press('Home');
  await expectTrue('Home came back to My runs: only my runs remain', `document.activeElement.matches('[data-scope="mine"]') && !location.search.includes('scope=everyone') && document.querySelectorAll('a.run-row').length === 6`);

  log('Library: search by text');
  await tabUntil('search box', focusIs('input[type="search"]'));
  await type('dark');
  await press('Enter');
  await expectTrue('Enter searched: URL has q=dark and one row', `location.search.includes('q=dark') && document.querySelectorAll('a.run-row').length === 1`);

  log('Library: filter with the keyboard, then reach "no match" and clear it');
  await tabUntil('Search button', focusNamed('Search'));
  await tabUntil('Status select', focusIs('select[aria-label="Status"]'));
  await type('F'); // type-ahead picks "Failed"
  await expectTrue('status=failed is in the URL, no rows match', `location.search.includes('status=failed') && !!document.querySelector('.state-no-match')`);
  await tabUntil('Clear filters', focusNamed('Clear filters'));
  await press('Enter');
  await expectTrue('Clear filters emptied the URL query and restored all rows', `location.search === '' && document.querySelectorAll('a.run-row').length === 6`);

  log('Library: More filters opens from the keyboard');
  await tabUntil('More filters', focusNamed('More filters'));
  await press('Enter');
  await expectTrue('More filters is open', `document.querySelector('details.more-filters').open`);
  await tabUntil('Computer select', focusIs('select[aria-label="Computer"]'));

  log('Library: open a run');
  await tabUntil('first run row', focusIs('a.run-row'));
  await press('Enter');
  await expectTrue('viewer opened for the run', `location.pathname.startsWith('/runs/') && !!document.querySelector('[data-section="outcome"]')`);

  await checkRunKeyboard('Review');

  log('Runs: the list beside the open run opens another run from the keyboard');
  const openRun = await evaluate('location.pathname');
  await tabUntil('the open run in the list beside it', focusIs('.right-panel a.run-row[aria-current="page"]'));
  await expectTrue('the open run says Viewing', `document.activeElement.textContent.includes('Viewing')`);
  await tabUntil('another run in the list', `${focusIs('.right-panel a.run-row')} && !e.hasAttribute('aria-current')`);
  await press('Enter');
  await expectTrue('Enter opened a different run beside the list', `location.pathname.startsWith('/runs/') && location.pathname !== ${JSON.stringify(openRun)} && !!document.querySelector('[data-section="outcome"]') && !!document.querySelector('.right-panel a.run-row')`);

  log('Viewer: back to the library');
  await tabUntil('Back to runs', focusNamed('Back to runs'), { back: true });
  await press('Enter');
  await expectTrue('back on /runs with the list', `location.pathname === '/runs' && document.querySelectorAll('a.run-row').length === 6`);

  const current = RUNS[0];
  mock.sessions = [{ session_id: current.session_id, initial_goal: current.prompt, status: current.status,
    start_time: current.start_time, end_time: current.end_time, device_serial: current.device_ref.serial }];
  mock.status = { status: 'idle', session_id: null };
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('Workspace shows the same run with its new-task box', `!!document.querySelector('app-run-view [data-section="outcome"]') && !!document.querySelector('textarea.dock-textarea')`);
  await expectTrue('first Workspace visit opens What\'s New', `!!document.querySelector('dialog.whats-new-dialog[open]')`);
  await press('Escape');
  await expectTrue('Escape dismisses What\'s New', `!document.querySelector('dialog.whats-new-dialog[open]')`);
  await checkRunKeyboard('Live');

  log('Workspace: the phone chip opens and closes from the keyboard');
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('workspace loaded with the phone chip', `!!document.querySelector('app-workspace-device-chip button.chip')`);
  await tabUntil('phone chip', focusIs('app-workspace-device-chip button.chip'));
  await expectTrue('chip says there is no phone and the picker is closed', `document.activeElement.textContent.includes('No phone') && document.activeElement.getAttribute('aria-expanded') === 'false'`);
  await press('Enter');
  await expectTrue('Enter opened the picker and focus moved inside it', `document.activeElement.getAttribute('aria-expanded') === null && !!document.querySelector('app-workspace-device-chip .panel')?.contains(document.activeElement)`);
  await expectTrue('focus is on the first usable choice in the picker', `document.activeElement.matches('app-workspace-device-chip .panel button')`);
  await press('Escape');
  await expectTrue('Escape closed the picker and focus returned to the chip', `!document.querySelector('app-workspace-device-chip .panel') && document.activeElement.matches('app-workspace-device-chip button.chip') && document.activeElement.getAttribute('aria-expanded') === 'false'`);
  log(`  focus is now: ${await describeFocus()}`);

  log('Workspace: skip links reach the new-task box and the run list in a few keys');
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('workspace loaded with the skip links', `!!document.querySelector('button.skip-link')`);
  await tabUntil('Skip to new task', focusNamed('Skip to new task'));
  await expectTrue('the skip link shows itself while focused', `document.activeElement.getBoundingClientRect().height >= 44`);
  await press('Enter');
  await expectTrue('Enter put focus in the new-task box', `document.activeElement.matches('textarea.dock-textarea')`);
  await tabUntil('Skip to run list', focusNamed('Skip to run list'), { back: true, max: 80 });
  await press('Enter');
  await expectTrue('Enter put focus on the first tab of the list', `document.activeElement.matches('.right-panel [role="tab"]')`);

  log('Runs, one column (700 px): no skip link points at the hidden list');
  await send('Emulation.setDeviceMetricsOverride', { width: 700, height: 800, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: `${base}/runs/${RUNS[0].session_id}` });
  await expectTrue('the open run loaded in one column', `!!document.querySelector('[data-section="outcome"]')`);
  await expectTrue('the list beside the run is hidden', `!document.querySelector('.right-panel') || !document.querySelector('.right-panel').checkVisibility()`);
  const reachable = [];
  for (let i = 0; i < 12; i++) {
    await press('Tab');
    reachable.push(await describeFocus());
  }
  log(`  first 12 Tab stops: ${reachable.join(' | ')}`);
  if (reachable.some((name) => name.startsWith('Skip to run list'))) throw new Error('a skip link to the hidden run list is reachable');
  await expectTrue('every focused control is on screen', `!document.activeElement || document.activeElement === document.body || document.activeElement.checkVisibility()`);
  await tabUntil('Back to runs', focusNamed('Back to runs'), { back: true, max: 20 }).catch(() => tabUntil('Back to runs', focusNamed('Back to runs'), { max: 60 }));
  await send('Emulation.clearDeviceMetricsOverride');

  log('Workspace: Task Queue and Notes & Plans tabs, then the run list');
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('workspace loaded with the queue tabs', `!!document.querySelector('.tab-selector-btn[data-tab="tasks"]')`);
  await tabUntil('Task Queue tab', focusIs('.tab-selector-btn[data-tab="tasks"]'));
  await expectTrue('Task Queue is the selected tab and the only tab stop', `document.activeElement.getAttribute('aria-selected') === 'true' && document.querySelector('.tab-selector-btn[data-tab="notes"]').tabIndex === -1`);
  await press('ArrowRight');
  await expectTrue('ArrowRight selected Notes & Plans and moved focus to it', `document.activeElement.matches('.tab-selector-btn[data-tab="notes"]') && document.activeElement.getAttribute('aria-selected') === 'true'`);
  await press('ArrowLeft');
  await expectTrue('ArrowLeft came back to Task Queue', `document.activeElement.matches('.tab-selector-btn[data-tab="tasks"]') && document.activeElement.getAttribute('aria-selected') === 'true'`);
  await tabUntil('My runs tab in the run list', focusIs('[role="tab"][data-scope="mine"]'));
  await press('ArrowRight');
  await expectTrue("ArrowRight switched the list to Everyone's runs", `document.activeElement.matches('[data-scope="everyone"]') && location.search.includes('scope=everyone')`);
  await press('Home');
  await expectTrue('Home came back to My runs', `document.activeElement.matches('[data-scope="mine"]')`);

  log('Task dock: stays open, and clipboard keyboard operation');
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('empty task dock is expanded', `!!document.querySelector('.workspace-floating-bar-wrapper.is-expanded textarea') && getComputedStyle(document.querySelector('.expanded-card-content')).display !== 'none'`);
  const dockWidth = await evaluate(`document.querySelector('.floating-dock-card').getBoundingClientRect().width`);
  await press('Tab');
  await expectTrue('focus loss does not collapse or resize the dock', `!document.querySelector('.is-dormant') && document.querySelector('.floating-dock-card').getBoundingClientRect().width === ${dockWidth}`);
  await tabUntil('task textarea', focusIs('textarea.dock-textarea'));
  await evaluate(`(() => {
    window.dockPasteEvents = [];
    document.querySelector('textarea.dock-textarea').addEventListener('paste', event => {
      window.dockPasteEvents.push({
        trusted: event.isTrusted,
        types: Array.from(event.clipboardData?.types ?? []),
        files: Array.from(event.clipboardData?.files ?? []).map(file => ({ type: file.type, size: file.size }))
      });
    }, { capture: true });
  })()`);
  await send('Browser.grantPermissions', { permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite'], origin: base });
  await evaluate(`(async () => {
    const canvas = new OffscreenCanvas(1, 1);
    canvas.getContext('2d').fillRect(0, 0, 1, 1);
    const image = await canvas.convertToBlob({ type: 'image/png' });
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': image })]);
  })()`);
  await pasteShortcut(send);
  await sleep(120);
  await expectTrue('native image paste delivers a trusted PNG clipboard payload', `window.dockPasteEvents.length === 1 && window.dockPasteEvents[0].trusted && window.dockPasteEvents[0].files.length === 1 && window.dockPasteEvents[0].files[0].type === 'image/png' && window.dockPasteEvents[0].files[0].size > 0`);
  await expectTrue('platform paste shortcut of an image creates a preview', `document.querySelectorAll('ul.attached-images img').length === 1`);
  await tabUntil('Remove image', focusIs('button.btn-remove-image'));
  await press('Enter');
  await expectTrue('Enter removes the pasted preview', `!document.querySelector('ul.attached-images')`);
  await tabUntil('task textarea', focusIs('textarea.dock-textarea'));
  await evaluate(`navigator.clipboard.writeText('clipboard task text')`);
  await pasteShortcut(send);
  await sleep(120);
  await expectTrue('native text paste delivers a trusted text clipboard payload', `window.dockPasteEvents.length === 2 && window.dockPasteEvents[1].trusted && window.dockPasteEvents[1].types.includes('text/plain') && window.dockPasteEvents[1].files.length === 0`);
  await expectTrue('platform paste shortcut of text remains text without an attachment', `document.querySelector('textarea.dock-textarea').value === 'clipboard task text' && !document.querySelector('ul.attached-images')`);

  log('\nKeyboard walkthrough passed.');
  await cleanup(0);
} catch (error) {
  console.error(`\nKeyboard walkthrough FAILED: ${error.message}`);
  console.error(await evaluate(`JSON.stringify({focus: document.activeElement?.outerHTML, text: document.querySelector('app-run-view')?.textContent})`).catch(() => 'Page unavailable'));
  console.error('Dock paste evidence:', await evaluate(`JSON.stringify({ focus: document.activeElement?.outerHTML, events: window.dockPasteEvents })`).catch(() => 'unavailable'));
  await cleanup(1);
}
