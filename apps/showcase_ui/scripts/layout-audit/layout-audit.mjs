// Layout audit in real headless Chrome against the built UI + mock API (no device, no backend).
//
//   npx ng build --configuration development && npm run test:layout      (CHROME_BIN overrides the browser; SHOTS=dir saves screenshots)
//
// 1. Clearance matrix: for every page x viewport x phone state, no visible text may sit under the
//    floating nav, nothing may overflow the viewport sideways, and the nav must stay inside the viewport.
// 2. Accessibility: the connected-phone status stays in the accessibility tree at every width; the chip is >= 44px.
// 3. Scenarios: RunView controls and Task Queue / Notes & Plans sidebar across mock states
//    (idle / running / paused-with-error / sessions API error / video error) and route changes.
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, existsSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock } from '../keyboard-walkthrough/mock-api.mjs';

const dist = fileURLToPath(new URL('../../dist/frontend/browser', import.meta.url));
if (!existsSync(path.join(dist, 'index.html'))) {
  console.error('No build found. Run `npm run build` first (development configuration is fine).');
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const { server, url: base } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'layout-audit-'));
const port = 9222 + Math.floor(Math.random() * 2000);
const chrome = spawn(process.env.CHROME_BIN || 'google-chrome',
  ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
chrome.on('error', (e) => { console.error(`Could not start Chrome (${e.message}). Set CHROME_BIN.`); process.exit(2); });

let ws, nextId = 1;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (exceptionDetails) throw new Error(exceptionDetails.text + ' ' + (exceptionDetails.exception?.description ?? ''));
  return result.value;
};
const failures = [];
const fail = (where, what) => { failures.push(`${where}: ${what}`); console.log(`  FAIL ${where}: ${what}`); };
let shotDir = process.env.SHOTS;
if (shotDir) mkdirSync(shotDir, { recursive: true });
const shot = async (name) => {
  if (!shotDir) return;
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(path.join(shotDir, `${name}.png`), Buffer.from(data, 'base64'));
};
const open = async (route, width, height = 800) => {
  await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: width < 600 });
  await send('Page.navigate', { url: base + route });
  // Needs the development build: the `ng` debug global is how states are driven without a backend.
  for (let i = 0; i < 100; i++) {
    if (await evaluate(`!!window.ng && !!document.querySelector('app-nav-switcher .floating-nav-switcher')`).catch(() => false)) break;
    await sleep(100);
  }
  await sleep(500);
};
// The phone lives in the Workspace chip (the nav carries no phone control), so this only has an effect on /workspace.
const setPhone = (connected) => evaluate(`(() => { const el = document.querySelector('app-workspace-device-chip');
  if (!el) return;
  ng.getComponent(el).phone.relay.state.set(${connected ? "{ status: 'connected', serial: '127.0.0.1:35117', sessionId: 'audit', error: null }" : "{ status: 'idle', serial: null, sessionId: null, error: null }"});
  ng.applyChanges(el); })()`).then(() => sleep(200));

// Everything a user could read or click that is not in the nav but sits under it.
const UNDER_NAV = `(() => {
  const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
  const hit = (r) => r.width > 0 && r.height > 0 && r.left < nav.right && r.right > nav.left && r.top < nav.bottom && r.bottom > nav.top;
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    if (!n.textContent.trim() || n.parentElement.closest('.floating-nav-switcher,script,style,.cdk-visually-hidden,.material-symbols-outlined')) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    if ([...range.getClientRects()].some(hit)) out.push(n.textContent.trim().slice(0, 28));
  }
  return { nav: [Math.round(nav.left), Math.round(nav.right), Math.round(nav.bottom)], under: out,
    overflowX: document.documentElement.scrollWidth - innerWidth, navOffscreen: nav.right > innerWidth || nav.left < 0 };
})()`;

async function clearanceMatrix() {
  const widths = [1770, 1280, 1150, 1024, 900, 768, 560, 480, 375, 320];
  const routes = ['/workspace', '/runs', '/runs/00000001-5d7e-4a10-9c33-0e1f2a3b4c5d', '/setup', '/check'];
  for (const identity of [null, { body: { email: 'a.very.long.reviewer.name+qa@subdomain.example-company.test', admin: false, auth_mode: 'cloudflare' } }]) {
    mock.identity = identity;
    for (const route of routes) for (const phone of [false, true]) for (const width of widths) {
      const where = `${route} @${width}px phone=${phone}${identity ? ' long-email' : ''}`;
      await open(route, width);
      await setPhone(phone);
      const r = await evaluate(UNDER_NAV);
      if (r.under.length) fail(where, `text under nav: ${[...new Set(r.under)].join(' | ')}`);
      if (r.navOffscreen) fail(where, `nav outside viewport ${r.nav}`);
      if (r.overflowX > 0) fail(where, `page scrolls sideways by ${r.overflowX}px`);
    }
  }
  mock.identity = null;
  console.log(`clearance matrix: ${failures.length ? 'failures above' : 'all clear'}`);
}

// The connected-phone status must be in the accessibility tree at every width, and the chip must be a 44px target.
async function phoneStatusA11y() {
  await send('Accessibility.enable');
  for (const width of [1770, 1024, 761, 760, 560, 375, 320]) {
    const where = `a11y phone status @${width}px`;
    await open('/workspace', width);
    await setPhone(true);
    const { nodes } = await send('Accessibility.getFullAXTree');
    // role=status takes no name from content, so read the live region's text nodes.
    const byId = new Map(nodes.map((n) => [n.nodeId, n]));
    const region = nodes.find((n) => !n.ignored && n.role?.value === 'status' && (n.childIds ?? []).some((c) => /5117/.test(byId.get(c)?.name?.value ?? '')));
    if (!region) fail(where, 'no role=status live region naming the connected phone in the accessibility tree');
    else console.log(`  ok   ${where}: role=status text="${region.childIds.map((c) => byId.get(c)?.name?.value).filter(Boolean).join('')}"`);
    const size = await evaluate(`(() => { const r = document.querySelector('app-workspace-device-chip .chip')?.getBoundingClientRect(); return r ? [r.width, r.height] : null; })()`);
    if (!size) fail(where, 'the phone chip is missing');
    else if (size[0] < 44 || size[1] < 44) fail(where, `phone chip is ${size[0]}x${size[1]}, under 44x44`);
    const nav = await evaluate(UNDER_NAV);
    if (nav.navOffscreen || nav.overflowX > 0) fail(where, 'phone chip brought back nav/page overflow');
  }
}

const T0 = Math.floor(Date.now() / 1000) - 600;
const longGoal = 'Open the settings app, scroll to the accessibility section and verify that every toggle reflects the saved state after a restart';
const sessions = [
  { session_id: 'aaaaaaaa-1', initial_goal: longGoal, start_time: T0, status: 'running', device_serial: 'emulator-5554' },
  { session_id: 'bbbbbbbb-2', initial_goal: 'Short goal', start_time: T0 - 900, end_time: T0 - 800, status: 'completed' },
  { session_id: 'cccccccc-3', initial_goal: 'Failing goal', start_time: T0 - 1800, end_time: T0 - 1700, status: 'failed' }
];
// Every state starts from the same pristine mock, so one state's flags can never leak into the next run.
const PRISTINE = { sessions: [], status: null, failSessions: false, failVideo: false, identity: null };
const STATES = {
  idle: () => Object.assign(mock, PRISTINE),
  'empty-api-error': () => Object.assign(mock, PRISTINE, { failSessions: true }),
  running: () => Object.assign(mock, PRISTINE, { sessions, status: { status: 'running', session_id: 'aaaaaaaa-1', goal: longGoal,
    queue: [{ session_id: 'dddddddd-4', goal: longGoal + ' (queued)', status: 'pending' }, 'plain string task'], active_tasks: [] } }),
  'paused-error': () => Object.assign(mock, PRISTINE, { sessions, failVideo: true, status: { status: 'paused', session_id: 'aaaaaaaa-1', goal: longGoal,
    paused_error: 'AI model request failed: 429 Too Many Requests from the provider. '.repeat(3), queue: [] } })
};
const rect = (sel) => `(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null; const r = e.getBoundingClientRect();
  return { l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom), sw: e.scrollWidth - e.clientWidth }; })()`;
const inside = (r) => r && r.l >= 0 && r.t >= 0;
const clickByText = (sel, text) => evaluate(`(() => { const e = [...document.querySelectorAll(${JSON.stringify(sel)})].find(e => e.textContent.includes(${JSON.stringify(text)})); if (!e) return false; e.click(); return true; })()`);

async function scenarios() {
  for (const [state, apply] of Object.entries(STATES)) for (const width of [1770, 1280, 1150, 1024, 768, 375, 320]) {
    apply();
    const where = `state=${state} @${width}px`;
    await open('/workspace', width);
    await setPhone(state !== 'idle');
    await sleep(2400); // status poll (2 s) + sessions fetch
    // Controls in RunView and the chat header must never sit under the nav.
    const covered = await evaluate(`(() => { const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
      return [...document.querySelectorAll('app-run-view button, app-run-view a, app-chat-interface .chat-header button')]
        .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.left < nav.right && r.right > nav.left && r.top < nav.bottom && r.bottom > nav.top; })
        .map((e) => (e.getAttribute('aria-label') || e.textContent).trim().slice(0, 24)); })()`);
    if (covered.length) fail(where, `page controls under the nav: ${covered.join(' | ')}`);
    const err = await evaluate(`document.body.innerText.includes('Failed to fetch') || document.body.innerText.includes('Something went wrong')`);
    if (err) fail(where, 'raw error text shown');
    const panel = await evaluate(rect('app-chat-interface'));
    const sw = await evaluate(`[...document.querySelectorAll('app-chat-interface *')].filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX === 'visible' && e.clientWidth > 0).slice(0, 3).map(e => e.className || e.tagName)`);
    if (!panel) fail(where, 'chat panel missing');
    else if (!inside(panel) || panel.r > width) fail(where, `chat panel outside viewport horizontally: ${JSON.stringify(panel)}`);
    else if (sw.length) fail(where, `chat panel content overflows its box: ${sw.join(', ')}`);
    for (const [tab, content] of [['Notes & Plans', '.notes-tab-content'], ['Task Queue', '.queue-section-header']]) {
      if (!(await clickByText('app-chat-interface button.tab-selector-btn', tab))) { fail(where, `${tab} tab not found`); continue; }
      await sleep(300);
      if (!(await evaluate(`!!document.querySelector(${JSON.stringify('app-chat-interface ' + content)})`))) fail(where, `${tab} content did not open`);
    }
    await shot(`chat-${state}-${width}`);
  }
  console.log(`scenarios: ${failures.length ? 'failures above' : 'all clear'}`);
}

try {
  for (let i = 0; i < 50 && !ws; i++) {
    try {
      const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page');
      if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((a, b) => { ws.onopen = a; ws.onerror = b; }); }
    } catch { await sleep(100); }
  }
  if (!ws) throw new Error('could not reach Chrome');
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
  };
  await send('Page.enable');
  const only = process.argv[2];
  if (only && !['clearance', 'a11y', 'scenarios'].includes(only)) throw new Error(`unknown audit phase "${only}" (clearance | a11y | scenarios)`);
  if (!only || only === 'clearance') await clearanceMatrix();
  if (!only || only === 'a11y') await phoneStatusA11y();
  if (!only || only === 'scenarios') await scenarios();
} catch (e) {
  console.error(e);
  failures.push(String(e));
}
ws?.close(); chrome.kill(); server.close(); await sleep(200);
rmSync(profile, { recursive: true, force: true });
console.log(failures.length ? `\nLayout audit FAILED: ${failures.length} finding(s)` : '\nLayout audit passed.');
process.exit(failures.length ? 1 : 0);
