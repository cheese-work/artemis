// Keyboard-only walkthrough of the run library and viewer, run in real headless Chrome.
// Every interaction is a trusted key press sent through the DevTools protocol
// (Tab, Shift+Tab, Enter, Escape, typing); the page is never clicked or scripted.
//
//   npm run build && npm run test:keyboard      (CHROME_BIN overrides the browser)
//
// Prints a transcript of what had focus after each key and exits 1 on the first miss.
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock } from './mock-api.mjs';
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

mock.readiness = {
  os_type: 'linux', overall_ready: true,
  probes: [{ id: 'android_adb', status: 'pass', metadata: { devices: [{ serial: 'keyboard-fixture', state: 'device', model: 'Pixel test', device_kind: 'phone' }] } }]
};
const { server, url: base } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'kbd-walk-'));
const port = await freePort();
const chrome = spawn(
  process.env.CHROME_BIN || 'google-chrome',
  ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, '--window-size=1280,900', 'about:blank'],
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

async function cleanup(code) {
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
  ArrowUp: { key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38 }
};
async function press(name, modifiers = 0) {
  const k = KEYS[name];
  await send('Input.dispatchKeyEvent', { type: k.text ? 'keyDown' : 'rawKeyDown', modifiers, ...k });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', modifiers, key: k.key, code: k.code, windowsVirtualKeyCode: k.windowsVirtualKeyCode });
  await sleep(120);
}
async function type(text) {
  for (const ch of text) {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ch, text: ch, unmodifiedText: ch });
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch });
    await sleep(40);
  }
  await sleep(150);
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
  await send('Page.navigate', { url: `${base}/runs` });
  await expectTrue('library loaded', `document.querySelectorAll('a.run-row').length === 6`);

  log('Library: the admin All users switch');
  await tabUntil('All users switch', focusIs('button[role="switch"]'));
  await press('Space');
  await expectTrue('Space turned All users on: the other QA\'s run is listed with its owner', `document.activeElement.getAttribute('aria-checked') === 'true' && document.querySelectorAll('a.run-row').length === 7 && document.body.textContent.includes('other@example.test')`);
  await press('Enter');
  await expectTrue('Enter turned it off: only my runs remain', `document.activeElement.getAttribute('aria-checked') === 'false' && document.querySelectorAll('a.run-row').length === 6`);

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

  log('Viewer: steps, share notice, download notice, technical details');
  await tabUntil('first step', focusIs('button.step-button'));
  await press('Enter');
  await expectTrue('Enter selected the step', `document.activeElement.getAttribute('aria-current') === 'step'`);
  await tabUntil('Copy link', focusNamed('Copy link'));
  await press('Enter');
  await expectTrue('share dialog is open, focus is inside it', `!!document.querySelector('dialog.trust-dialog[open]') && document.querySelector('dialog.trust-dialog').contains(document.activeElement)`);
  await expectTrue('share dialog shows both notices', `document.querySelector('dialog.trust-dialog').textContent.includes('not redacted') && document.querySelector('dialog.trust-dialog').textContent.includes('Cloudflare Access')`);
  await press('Escape');
  await expectTrue('Escape closed the dialog and focus returned to Copy link', `!document.querySelector('dialog.trust-dialog[open]') && ${`(() => { const e = document.activeElement; return ${focusNamed('Copy link')}; })()`}`);
  log(`  focus is now: ${await describeFocus()}`);
  await tabUntil('Download', focusNamed('Download'));
  await press('Enter');
  await expectTrue('download dialog shows only the redaction notice', `!!document.querySelector('dialog.trust-dialog[open]') && document.querySelector('dialog.trust-dialog').textContent.includes('not redacted') && !document.querySelector('dialog.trust-dialog').textContent.includes('Cloudflare Access')`);
  await press('Escape');
  await expectTrue('Escape closed it and focus returned to Download', `!document.querySelector('dialog.trust-dialog[open]') && ${`(() => { const e = document.activeElement; return ${focusNamed('Download')}; })()`}`);
  await tabUntil('Pin', focusNamed('Pin'));
  await tabUntil('Technical details', focusNamed('Technical details'));
  await press('Enter');
  await expectTrue('Technical details opened and raw logs rendered', `!!document.querySelector('pre.raw-logs')`);

  log('Viewer: back to the library');
  await tabUntil('Back to runs', focusNamed('Back to runs'), { back: true });
  await press('Enter');
  await expectTrue('back on /runs with the list', `location.pathname === '/runs' && document.querySelectorAll('a.run-row').length === 6`);

  log('Workspace: the phone chip opens and closes from the keyboard');
  await send('Page.navigate', { url: `${base}/workspace` });
  await expectTrue('workspace loaded with the phone chip', `!!document.querySelector('app-workspace-device-chip button.chip')`);
  await tabUntil('phone chip', focusIs('app-workspace-device-chip button.chip'));
  await expectTrue('chip says there is no phone and the picker is closed', `document.activeElement.textContent.includes('No phone') && document.activeElement.getAttribute('aria-expanded') === 'false'`);
  await press('Enter');
  await expectTrue('Enter opened the picker and focus moved inside it', `document.activeElement.getAttribute('aria-expanded') === null && !!document.querySelector('app-workspace-device-chip .panel')?.contains(document.activeElement)`);
  await expectTrue('focus is on the first usable choice: Connect a phone from this browser', `document.activeElement.textContent.includes('Connect a phone from this browser')`);
  await press('Escape');
  await expectTrue('Escape closed the picker and focus returned to the chip', `!document.querySelector('app-workspace-device-chip .panel') && document.activeElement.matches('app-workspace-device-chip button.chip') && document.activeElement.getAttribute('aria-expanded') === 'false'`);
  log(`  focus is now: ${await describeFocus()}`);

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
  console.error('Dock paste evidence:', await evaluate(`JSON.stringify({ focus: document.activeElement?.outerHTML, events: window.dockPasteEvents })`).catch(() => 'unavailable'));
  await cleanup(1);
}
