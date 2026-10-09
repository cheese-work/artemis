// Layout audit in real headless Chrome against the built UI + mock API (no device, no backend).
//
//   npx ng build --configuration development && npm run test:layout      (CHROME_BIN overrides the browser; SHOTS=dir saves screenshots)
//
// 1. Clearance matrix: for every page x viewport x phone state, no visible text may sit under the
//    floating nav, nothing may overflow the viewport sideways, and the nav must stay inside the viewport.
// 2. Accessibility: the connected-phone status stays in the accessibility tree at every width; the chip is >= 44px.
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
  ['--headless=new', '--no-sandbox', '--password-store=basic', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
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
    if (!n.textContent.trim() || n.parentElement.closest('.floating-nav-switcher,script,style,.cdk-visually-hidden,.material-symbols-outlined,.skip-link:not(:focus)')) continue;
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
    const covered = await evaluate(`(() => { const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
      return [...document.querySelectorAll('app-run-view button, app-run-view a, app-run-library button')]
        .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.left < nav.right && r.right > nav.left && r.top < nav.bottom && r.bottom > nav.top; })
        .map((e) => (e.getAttribute('aria-label') || e.textContent).trim().slice(0, 24)); })()`);
    if (covered.length) fail(where, `page controls under the nav: ${covered.join(' | ')}`);
    const err = await evaluate(`document.body.innerText.includes('Failed to fetch') || document.body.innerText.includes('Something went wrong')`);
    if (err) fail(where, 'raw error text shown');
    const panel = await evaluate(rect('.run-list-pane'));
    if (!panel) fail(where, 'run list pane missing');
    if (width >= 1024) {
      if (!inside(panel) || panel.r > width) fail(where, `run list pane outside viewport horizontally: ${JSON.stringify(panel)}`);
      for (const tab of ['My runs', "Everyone's runs"]) {
        if (!(await clickByText('app-run-library button[role=tab]', tab))) fail(where, `${tab} tab not found`);
        await sleep(300);
        if (!(await evaluate(`document.querySelector('app-run-library [aria-selected=true]')?.textContent.trim() === ${JSON.stringify(tab)}`))) fail(where, `${tab} did not select`);
      }
    }
    await shot(`workspace-${state}-${width}`);
  }
  console.log(`scenarios: ${failures.length ? 'failures above' : 'all clear'}`);
}

// Contrast of every text run against the opaque background it actually sits on (WCAG relative luminance).
const CONTRAST = `(() => {
  const parse = (c) => { const m = c.match(/rgba?\\(([^)]+)\\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p[3] ?? 1 }; };
  const lin = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  const lum = ({ r, g, b }) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
  const over = (top, under) => ({ r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1 });
  // CSS opacity fades a whole group: the element's background, its text and everything inside it together.
  // Paint the chain root-first. Each node paints its background, then its subtree, then blends that result over what is
  // behind it by its own opacity: lerp(under, painted, opacity). Run once with the text and once without it.
  const chain = (el) => { const nodes = []; for (let e = el; e; e = e.parentElement) nodes.unshift(e); return nodes; };
  const lerp = (under, painted, o) => ({ r: under.r + (painted.r - under.r) * o, g: under.g + (painted.g - under.g) * o, b: under.b + (painted.b - under.b) * o, a: 1 });
  const paint = (nodes, under, text) => {
    if (!nodes.length) return text ? over(text, under) : under;
    const cs = getComputedStyle(nodes[0]);
    const bg = parse(cs.backgroundColor);
    const base = bg && bg.a > 0 ? over(bg, under) : under;
    return lerp(under, paint(nodes.slice(1), base, text), +cs.opacity);
  };
  const out = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const el = n.parentElement;
    if (!n.textContent.trim() || el.closest('script,style,.material-symbols-outlined,.cdk-visually-hidden,.sr-only,[disabled],[aria-disabled=true]')) continue;
    const range = document.createRange(); range.selectNodeContents(n);
    if (![...range.getClientRects()].some((r) => r.width > 0 && r.height > 0)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || +cs.opacity === 0) continue;
    const fg = parse(cs.color); if (!fg) continue;
    const nodes = chain(el); const page = { r: 255, g: 255, b: 255, a: 1 };
    const bg = paint(nodes, page, null); const text = paint(nodes, page, fg);
    const [hi, lo] = [lum(text), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const large = parseFloat(cs.fontSize) >= 24 || (parseFloat(cs.fontSize) >= 18.66 && +cs.fontWeight >= 700);
    if (ratio < (large ? 3 : 4.5)) out.push((el.className?.toString() || el.tagName).slice(0, 40) + ' "' + n.textContent.trim().slice(0, 24) + '" ' + ratio.toFixed(2) + ':1');
  }
  return [...new Set(out)];
})()`;

// The audit must see what it claims to see. Each probe is real DOM with a known right answer; a miss fails the contract pass.
//  - faded label: CSS opacity on the text itself (2.7:1).
//  - grouped background: white text on a navy panel, the whole panel at opacity .5 (3.1:1). The group fades together, so
//    a check that fades only the text and keeps the panel navy reads 5.4:1 and misses it (CHE-1278 F3, second case).
//  - control: the same panel at full opacity must NOT be reported.
const CONTRAST_PROBES = [
  { label: 'faded label', flagged: true, html: '<span style="color:var(--color-text-faint);background:#fff;opacity:.7">faded label</span>' },
  { label: 'grouped background', flagged: true, html: '<div style="opacity:.5;background:#0f172a"><p style="margin:0;color:#fff">grouped background</p></div>' },
  { label: 'nested group', flagged: true, html: '<div style="opacity:.8"><div style="opacity:.6;background:#0f172a"><p style="margin:0;color:#fff">nested group</p></div></div>' },
  { label: 'control panel', flagged: false, html: '<div style="background:#0f172a"><p style="margin:0;color:#fff">control panel</p></div>' }
];
const CONTRAST_SELF_TEST = `(() => {
  const found = [];
  for (const probe of ${JSON.stringify(CONTRAST_PROBES)}) {
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;top:0;left:0;width:200px';
    host.innerHTML = probe.html;
    document.body.appendChild(host);
    const reported = (${CONTRAST}).some((m) => m.includes(probe.label));
    host.remove();
    if (reported !== probe.flagged) found.push(probe.label + (probe.flagged ? ' was not reported' : ' was reported'));
  }
  return found;
})()`;

const BLUR = `[...document.querySelectorAll('*')].filter((e) => { const c = getComputedStyle(e); return (c.backdropFilter && c.backdropFilter !== 'none') || /blur/.test(c.filter); }).map((e) => e.className?.toString() || e.tagName).slice(0, 5)`;
const TARGETS = `[...document.querySelectorAll('.floating-nav-switcher .nav-tab-btn, .tab-selector-btn, .composer-btn, .composer .segment, app-run-library button, app-run-library summary, app-run-library input, app-run-library select, app-run-view .action-button, app-run-view .secondary-button, app-run-view summary')]
  .map((e) => [e, e.getBoundingClientRect()]).filter(([e, r]) => r.width > 0 && r.height > 0 && !e.disabled)
  .filter(([, r]) => r.height < 43.5 || r.width < 43.5).map(([e, r]) => (e.getAttribute('aria-label') || e.textContent || e.className).trim().slice(0, 24) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height))`;
const BOXES = `(() => { const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width, h: b.height }; };
  return { list: r('.run-list-pane'), detail: r('.detail-pane'), run: r('.run-surface'), dock: r('.composer'), scrollH: document.documentElement.scrollHeight, overflowX: document.documentElement.scrollWidth - innerWidth }; })()`;

function checkPanes(where, route, width, b) {
  const TOLERANCE = 2;
  if (!b.list || !b.detail) return fail(where, 'run or list pane missing');
  if (width >= 1024) {
    if (Math.abs(b.list.w - 360) > TOLERANCE || Math.abs(b.list.r - b.detail.l) > TOLERANCE || Math.abs(b.list.t - b.detail.t) > TOLERANCE) {
      fail(where, `list is not 360px left of the detail: ${JSON.stringify({ list: b.list, detail: b.detail })}`);
    }
  } else if (route === '/runs') {
    if (b.list.w === 0 || b.detail.w > 0) fail(where, 'only the run list should be visible');
  } else if (b.detail.w === 0 || b.list.w > 0) {
    fail(where, 'only the detail should be visible');
  }
  if (!b.dock) return;
  if (b.dock.b > 800 + TOLERANCE) fail(where, `new-task box is off screen: dock bottom ${b.dock.b}`);
  if (b.run && b.dock.t < b.run.b - TOLERANCE) fail(where, `new-task box covers the run: dock top ${b.dock.t}, run bottom ${b.run.b}`);
}

async function contract() {
  for (const route of ['/workspace', '/runs/00000002-5d7e-4a10-9c33-0e1f2a3b4c5d', '/runs', '/setup']) {
    const panes = route === '/workspace' || route.startsWith('/runs');
    for (const width of [1440, 1200, 1024, 1023, 800, 390, 320]) {
      const where = `contract ${route.split('/').slice(0, 2).join('/')} @${width}px`;
      await open(route, width, 800);
      await setPhone(true);
      await sleep(400);
      const b = await evaluate(BOXES);
      if (b.overflowX > 0) fail(where, `page scrolls sideways by ${b.overflowX}px`);
      if (panes) checkPanes(where, route, width, b);
      const small = await evaluate(TARGETS);
      if (small.length) fail(where, `targets under 44px: ${small.join(' | ')}`);
      const blur = await evaluate(BLUR);
      if (blur.length) fail(where, `blur or glass on: ${blur.join(', ')}`);
      const low = await evaluate(CONTRAST);
      if (low.length) fail(where, `text under 4.5:1 on its background: ${low.slice(0, 6).join(' | ')}`);
      await shot(`contract-${route.split('/')[1]}-${width}`);
    }
  }
  await open('/workspace', 1280);
  for (const miss of await evaluate(CONTRAST_SELF_TEST)) fail('contract audit self-test', `${miss}: the contrast check does not compose CSS opacity correctly`);
  // Open menus sit on their own surfaces: the phone picker and the user menu.
  await open('/workspace', 1280);
  for (const [name, selector] of [['phone picker', 'app-workspace-device-chip button.chip'], ['user menu', 'summary[aria-label^="User menu"]']]) {
    if (!(await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return false; e.click(); return true; })()`))) { fail(`contract ${name}`, 'control not found'); continue; }
    await sleep(300);
    const low = await evaluate(CONTRAST);
    if (low.length) fail(`contract ${name} open`, `text under 4.5:1 on its background: ${low.slice(0, 6).join(' | ')}`);
    await shot(`contract-${name.replace(' ', '-')}-open`);
    await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
    await open('/workspace', 1280);
  }
  // Reduced motion: nothing keeps animating.
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  await open('/workspace', 1280);
  const moving = await evaluate(`[...document.querySelectorAll('*')].filter((e) => { const c = getComputedStyle(e); return c.animationName !== 'none' && parseFloat(c.animationDuration) > 0.05 && c.animationIterationCount === 'infinite'; }).map((e) => e.className?.toString() || e.tagName).slice(0, 5)`);
  if (moving.length) fail('contract reduced motion', `still animating: ${moving.join(', ')}`);
  await send('Emulation.setEmulatedMedia', { features: [] });
  console.log(`contract: ${failures.length ? 'failures above' : 'all clear'}`);
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
  if (only && !['clearance', 'a11y', 'scenarios', 'contract'].includes(only)) throw new Error(`unknown audit phase "${only}" (clearance | a11y | scenarios | contract)`);
  if (!only || only === 'clearance') await clearanceMatrix();
  if (!only || only === 'a11y') await phoneStatusA11y();
  if (!only || only === 'scenarios') await scenarios();
  if (!only || only === 'contract') await contract();
} catch (e) {
  console.error(e);
  failures.push(String(e));
}
ws?.close(); chrome.kill(); server.close(); await sleep(200);
rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
console.log(failures.length ? `\nLayout audit FAILED: ${failures.length} finding(s)` : '\nLayout audit passed.');
process.exit(failures.length ? 1 : 0);
