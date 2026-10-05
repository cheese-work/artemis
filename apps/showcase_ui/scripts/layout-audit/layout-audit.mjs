// Layout audit in real headless Chrome against the built UI + mock API (no device, no backend).
//
//   npx ng build --configuration development && npm run test:layout      (CHROME_BIN overrides the browser; SHOTS=dir saves screenshots)
//
// 1. Clearance matrix: for every page x viewport x phone-badge state, no visible text may sit under the
//    floating nav, nothing may overflow the viewport sideways, and the nav must stay inside the viewport.
// 2. Accessibility: the connected-phone status stays in the accessibility tree at every width.
// 3. Scenarios: Task Queue dropdown, right-hand chat panel, floating video player, across mock states
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
const setPhone = (connected) => evaluate(`(() => { const el = document.querySelector('app-nav-switcher');
  ng.getComponent(el).usbRelay.state.set(${connected ? "{ status: 'connected', serial: '127.0.0.1:35117', error: null }" : "{ status: 'idle', serial: null, error: null }"});
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

// The connected-phone status must be in the accessibility tree at every width, including where its text is visually hidden.
async function phoneStatusA11y() {
  await send('Accessibility.enable');
  for (const width of [1770, 1024, 761, 760, 560, 375, 320]) {
    const where = `a11y phone status @${width}px`;
    await open('/workspace', width);
    await setPhone(true);
    const { nodes } = await send('Accessibility.getFullAXTree');
    // role=status takes no name from content, so read the live region's text nodes.
    const byId = new Map(nodes.map((n) => [n.nodeId, n]));
    const region = nodes.find((n) => !n.ignored && n.role?.value === 'status' && (n.childIds ?? []).some((c) => /Phone connected/.test(byId.get(c)?.name?.value ?? '')));
    if (!region) fail(where, 'no role=status live region containing "Phone connected…" in the accessibility tree');
    else console.log(`  ok   ${where}: role=status text="${region.childIds.map((c) => byId.get(c)?.name?.value).filter(Boolean).join('')}"`);
    const hidden = await evaluate(`(() => { const l = document.querySelector('.usb-relay-badge .badge-label'); return getComputedStyle(l).display === 'none' || getComputedStyle(l).visibility === 'hidden'; })()`);
    if (hidden) fail(where, 'status label is display:none / visibility:hidden');
    const nav = await evaluate(UNDER_NAV);
    if (nav.navOffscreen || nav.overflowX > 0) fail(where, 'status label brought back nav/page overflow');
  }
}

const T0 = Math.floor(Date.now() / 1000) - 600;
const longGoal = 'Open the settings app, scroll to the accessibility section and verify that every toggle reflects the saved state after a restart';
const sessions = [
  { session_id: 'aaaaaaaa-1', initial_goal: longGoal, start_time: T0, status: 'running', device_serial: 'emulator-5554' },
  { session_id: 'bbbbbbbb-2', initial_goal: 'Short goal', start_time: T0 - 900, end_time: T0 - 800, status: 'completed' },
  { session_id: 'cccccccc-3', initial_goal: 'Failing goal', start_time: T0 - 1800, end_time: T0 - 1700, status: 'failed' }
];
const STATES = {
  idle: () => Object.assign(mock, { sessions: [], status: null, failSessions: false, failVideo: false }),
  'empty-api-error': () => Object.assign(mock, { sessions: [], status: null, failSessions: true }),
  running: () => Object.assign(mock, { sessions, failSessions: false, status: { status: 'running', session_id: 'aaaaaaaa-1', goal: longGoal,
    queue: [{ session_id: 'dddddddd-4', goal: longGoal + ' (queued)', status: 'pending' }, 'plain string task'], active_tasks: [] } }),
  'paused-error': () => Object.assign(mock, { sessions, failSessions: false, failVideo: true, status: { status: 'paused', session_id: 'aaaaaaaa-1', goal: longGoal,
    paused_error: 'AI model request failed: 429 Too Many Requests from the provider. '.repeat(3), queue: [] } })
};
const rect = (sel) => `(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null; const r = e.getBoundingClientRect();
  return { l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom), sw: e.scrollWidth - e.clientWidth }; })()`;
const inside = (r) => r && r.l >= 0 && r.t >= 0;
const clickByText = (sel, text) => evaluate(`(() => { const e = [...document.querySelectorAll(${JSON.stringify(sel)})].find(e => e.textContent.includes(${JSON.stringify(text)})); if (!e) return false; e.click(); return true; })()`);

// Player frame and every header control: inside the viewport and clear of the nav (the nav may be 1-3 rows tall).
async function playerClear(where, phase) {
  const r = await evaluate(`(() => { const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
    const w = document.querySelector('.floating-video-wrapper'); if (!w) return null; const f = w.getBoundingClientRect();
    const over = (r) => r.width > 0 && r.left < nav.right && r.right > nav.left && r.top < nav.bottom && r.bottom > nav.top;
    return { nav: [nav.left, nav.top, nav.right, nav.bottom].map(Math.round), frame: [f.left, f.top, f.right, f.bottom].map(Math.round), vw: innerWidth, vh: innerHeight,
      frameOver: over(f), controls: [...w.querySelectorAll('.window-header button')].filter((b) => over(b.getBoundingClientRect())).map((b) => (b.getAttribute('aria-label') || b.getAttribute('title') || b.textContent).trim().slice(0, 20)) }; })()`);
  if (!r) return fail(where, `${phase}: floating player gone`);
  const [l, t, rt, b] = r.frame;
  if (l < 0 || t < 0 || rt > r.vw || b > r.vh) fail(where, `${phase}: player outside viewport ${r.frame}`);
  if (r.frameOver) fail(where, `${phase}: player frame ${r.frame} overlaps nav ${r.nav}`);
  if (r.controls.length) fail(where, `${phase}: nav covers player controls: ${r.controls.join(', ')}`);
}

async function scenarios() {
  for (const [state, apply] of Object.entries(STATES)) for (const width of [1770, 1280, 1150, 1024, 768, 375, 320]) {
    apply();
    const where = `state=${state} @${width}px`;
    await open('/workspace', width);
    await setPhone(state !== 'idle');
    await sleep(2400); // status poll (2 s) + sessions fetch
    // Controls that live in the page chrome (stream toolbar, header tabs, chat header) must never sit under the nav.
    const covered = await evaluate(`(() => { const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
      return [...document.querySelectorAll('.stream-floating-toolbar button, .stream-floating-toolbar a, .vscode-nav-header button, app-chat-interface .chat-header button')]
        .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.left < nav.right && r.right > nav.left && r.top < nav.bottom && r.bottom > nav.top; })
        .map((e) => (e.getAttribute('aria-label') || e.textContent).trim().slice(0, 24)); })()`);
    if (covered.length) fail(where, `page controls under the nav: ${covered.join(' | ')}`);
    const err = await evaluate(`document.body.innerText.includes('Failed to fetch') || document.body.innerText.includes('Something went wrong')`);
    if (err) fail(where, 'raw error text shown');
    if (width > 1150) {
      // Chat panel: must show its tabs and keep long task text inside the panel.
      const panel = await evaluate(rect('app-chat-interface'));
      const sw = await evaluate(`[...document.querySelectorAll('app-chat-interface *')].filter(e => e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).overflowX === 'visible' && e.clientWidth > 0).slice(0, 3).map(e => e.className || e.tagName)`);
      if (!panel) fail(where, 'chat panel missing'); else if (sw.length) fail(where, `chat panel content overflows its box: ${sw.join(', ')}`);
      await shot(`chat-${state}-${width}`);
    } else {
      // Task Queue dropdown: opens inside the viewport, is closable with Escape, route change closes it.
      if (!(await clickByText('button.vscode-tab-btn', width <= 900 ? '' : 'Task Queue'))) { fail(where, 'Task Queue tab not found'); continue; }
      await sleep(300);
      const menu = await evaluate(rect('.vscode-dropdown-menu'));
      if (!menu) fail(where, 'Task Queue dropdown did not open');
      else {
        if (!inside(menu) || menu.r > width || menu.b > 800) fail(where, `dropdown outside viewport ${JSON.stringify(menu)}`);
        const under = await evaluate(UNDER_NAV);
        if (menu.t < under.nav[2] && menu.r > under.nav[0] && menu.l < under.nav[1]) fail(where, `dropdown top ${menu.t} is under nav (bottom ${under.nav[2]})`);
        await shot(`dropdown-${state}-${width}`);
        await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
        await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
        await sleep(300);
        if (await evaluate(`!!document.querySelector('.vscode-dropdown-menu')`)) fail(where, 'Escape did not close the Task Queue dropdown');
        await clickByText('button.vscode-tab-btn', width <= 900 ? '' : 'Task Queue');
        await sleep(300);
        await evaluate(`document.querySelector('a[href="/runs"]').click()`);
        await sleep(500);
        if (await evaluate(`!!document.querySelector('.vscode-dropdown-menu')`)) fail(where, 'dropdown stayed open after route change');
        await open('/workspace', width);
        await setPhone(state !== 'idle'); // a reload resets the browser-side phone state: restore it before the player checks
        await sleep(300);
      }
    }
    // Floating player: live / error states, at the default position, minimized, theater, and after a route change.
    const opened = await evaluate(`(() => { const svc = ng.getComponent(document.querySelector('app-floating-video-player')).agentService;
      svc.openVideoPlayer('aaaaaaaa-1'); ng.applyChanges(document.querySelector('app-floating-video-player')); return !!svc; })()`).catch(() => false);
    await sleep(800);
    if (!opened) { fail(where, 'could not open floating player'); continue; }
    const pw = await evaluate(rect('.floating-video-wrapper'));
    if (!pw) fail(where, 'floating player did not render');
    else {
      const hdrOverflow = await evaluate(`(() => { const h = document.querySelector('.floating-video-wrapper .window-header'); return [...h.querySelectorAll('button')].filter(b => b.getBoundingClientRect().right > h.getBoundingClientRect().right + 1).length; })()`);
      if (hdrOverflow) fail(where, `${hdrOverflow} player header buttons overflow the header`);
      if (state !== 'idle' && !(await evaluate(`ng.getComponent(document.querySelector('app-nav-switcher')).usbRelay.state().status === 'connected'`))) fail(where, 'phone-connected state was lost before the player check');
      await playerClear(where, 'on open');
      await shot(`player-${state}-${width}`);
      for (const route of ['/runs', '/workspace']) {
        await evaluate(`document.querySelector('a[href="${route}"]').click()`);
        await sleep(600);
        await playerClear(where, `after route to ${route}`);
      }
    }
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
