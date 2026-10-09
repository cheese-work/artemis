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

const [outDir = 'screenshots', label = 'shot'] = process.argv.slice(2);
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
await send("Page.enable");
const T0 = Math.floor(Date.now() / 1000) - 600;
mock.readiness = { os_type: 'linux', overall_ready: true, probes: [{ id: 'system_config', status: 'pass' }, { id: 'llm_api_key', status: 'pass', metadata: { is_set: true } }, { id: 'android_adb', status: 'pass', metadata: { devices: [] } }] };
mock.sessions = [
  { session_id: 'aaaaaaaa-1', initial_goal: 'Open settings and verify the accessibility toggles', start_time: T0, end_time: T0 + 90, status: 'completed' },
  { session_id: 'bbbbbbbb-2', initial_goal: 'Add an item to the cart and check out as a guest', start_time: T0 - 900, end_time: T0 - 800, status: 'failed' }
];
const pages = { workspace: '/workspace', runs: '/runs', 'run-detail': `/runs/${RUNS[1].session_id}`, setup: '/setup' };
for (const width of [1280, 1024]) {
  for (const [name, route] of Object.entries(pages)) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 800, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: base + route });
    await sleep(2500);
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(path.join(outDir, `${label}-${name}-${width}.png`), Buffer.from(data, 'base64'));
    console.log(`${label}-${name}-${width}.png`);
  }
}
ws.close(); chrome.kill(); server.close();
await sleep(500);
rmSync(profile, { recursive: true, force: true, maxRetries: 5 });
