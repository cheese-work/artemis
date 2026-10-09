// Before/after evidence: Workspace and Runs at 1280 and 1024 px, against the built UI + mock API.
//
//   npx ng build --configuration development && node scripts/layout-audit/screenshots.mjs <outDir> [label]
//
// Writes <outDir>/<label>-<page>-<width>.png. CHROME_BIN overrides the browser.
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock, RUNS } from '../keyboard-walkthrough/mock-api.mjs';

const [outDir = 'screenshots', label = 'shot', audit] = process.argv.slice(2);
const checkControls = audit === '--controls';
const failures = [];
mkdirSync(outDir, { recursive: true });
const dist = fileURLToPath(new URL('../../dist/frontend/browser', import.meta.url));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const { server, url: base } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'shots-'));
const port = 9222 + Math.floor(Math.random() * 2000);
const chrome = spawn(process.env.CHROME_BIN || 'google-chrome',
  ['--headless=new', '--no-sandbox', '--password-store=basic', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });

let ws, nextId = 1;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
for (let i = 0; i < 50 && !ws; i++) {
  try {
    const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === 'page');
    if (page) { ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((a, b) => { ws.onopen = a; ws.onerror = b; }); }
  } catch { await sleep(100); }
}
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
};
const evaluate = async (expression) => {
  const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description ?? exceptionDetails.text);
  return result.value;
};
await send("Page.enable");
const T0 = Math.floor(Date.now() / 1000) - 600;
mock.readiness = { os_type: 'linux', overall_ready: true, probes: [{ id: 'system_config', status: 'pass' }, { id: 'llm_api_key', status: 'pass', metadata: { is_set: true } }, { id: 'android_adb', status: 'pass', metadata: { devices: [] } }] };
mock.sessions = [
  { session_id: 'aaaaaaaa-1', initial_goal: 'Open settings and verify the accessibility toggles', start_time: T0, end_time: T0 + 90, status: 'completed' },
  { session_id: 'bbbbbbbb-2', initial_goal: 'Add an item to the cart and check out as a guest', start_time: T0 - 900, end_time: T0 - 800, status: 'failed' }
];
if (checkControls) {
  mock.config = {
    version: 'control-audit',
    default: { provider: 'openai', model: 'fixture-model', fallback: { provider: 'openai', model: 'fixture-fallback' } },
    providers: [{ name: 'openai', configured: true, key_preview: 'fixture', key_source: 'fixture', base_url: null, base_url_source: 'default' }],
    sources: { default: 'fixture', env: 'fixture' }
  };
}
const pages = { workspace: '/workspace', runs: '/runs', 'run-detail': `/runs/${RUNS[1].session_id}`, setup: '/setup' };
if (checkControls) delete pages['run-detail'];
for (const width of checkControls ? [1440, 390] : [1280, 1024]) {
  for (const [name, route] of Object.entries(pages)) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 800, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: base + route });
    await sleep(2500);
    if (checkControls) {
      const errors = await evaluate(`(() => {
        const errors = [];
        const tokens = getComputedStyle(document.documentElement);
        const luminance = (name) => {
          const hex = tokens.getPropertyValue(name).trim().slice(1);
          const channels = [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255).map(channel => channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4);
          return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722;
        };
        const focus = luminance('--color-focus');
        for (const name of ['--color-bg', '--color-surface', '--color-surface-subtle', '--color-primary-tint']) {
          const background = luminance(name);
          const ratio = (Math.max(focus, background) + .05) / (Math.min(focus, background) + .05);
          if (!Number.isFinite(ratio) || ratio < 3) errors.push(name + ': focus contrast ' + ratio);
        }
        const visible = (element) => element.checkVisibility() && element.getBoundingClientRect().width > 0;
        for (const element of document.querySelectorAll('button, summary, input:not([type="file"]), select, textarea, .nav-tab-btn')) {
          if (!visible(element) || element.closest('.skip-links, .visually-hidden-input')) continue;
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          const label = element.className || element.textContent.trim().slice(0, 40) || element.tagName;
          if (rect.width < 44 || rect.height < 44) errors.push(label + ': hit box ' + rect.width + ' × ' + rect.height);
          if (element.matches('button, .nav-tab-btn')) {
            if (['Top', 'Right', 'Bottom', 'Left'].some(side => parseFloat(style['border' + side + 'Width']) > 0)) errors.push(label + ': outlined control');
            if (style.boxShadow !== 'none') errors.push(label + ': control shadow');
          }
        }
        for (const element of document.querySelectorAll('.floating-nav-switcher, app-setup .panel, app-home .guide-card, .model-profile-toggle, .os-selector-pills')) {
          if (visible(element) && getComputedStyle(element).boxShadow !== 'none') errors.push(element.className + ': surface shadow');
          if (visible(element) && parseFloat(getComputedStyle(element).borderTopWidth) > 0) errors.push(element.className + ': surface outline');
        }
        return errors;
      })()`);
      failures.push(...errors.map(error => `${name}-${width}: ${error}`));
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      const focusErrors = await evaluate(`(async () => {
        const errors = [];
        for (const element of document.querySelectorAll('button:not(:disabled), .nav-tab-btn')) {
          if (!element.checkVisibility() || element.closest('.skip-links')) continue;
          element.focus({ preventScroll: true });
          await new Promise(resolve => setTimeout(resolve, 200));
          const style = getComputedStyle(element);
          if (!element.matches(':focus-visible') || style.outlineStyle !== 'solid' || parseFloat(style.outlineWidth) < 3 || style.outlineColor !== 'rgb(67, 56, 202)') errors.push(element.className + ': missing focus ring (' + style.outline + ')');
        }
        document.activeElement?.blur();
        return errors;
      })()`);
      failures.push(...focusErrors.map(error => `${name}-${width}: ${error}`));
    }
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(path.join(outDir, `${label}-${name}-${width}.png`), Buffer.from(data, 'base64'));
    console.log(`${label}-${name}-${width}.png`);
    if (checkControls && name === 'setup') {
      await evaluate(`document.querySelector('.model-panel').scrollIntoView({ block: 'center' })`);
      const { data: panelData } = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(outDir, `${label}-setup-panels-${width}.png`), Buffer.from(panelData, 'base64'));
    }
  }
}
ws.close(); chrome.kill(); server.close();
await sleep(500);
rmSync(profile, { recursive: true, force: true, maxRetries: 5 });
if (failures.length) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else if (checkControls) {
  console.log('PASS: control borders, shadows, 44 px hit boxes, keyboard focus and 3:1 focus contrast at 1440 px and 390 px.');
}
