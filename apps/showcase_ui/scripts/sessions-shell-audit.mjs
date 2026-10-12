import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock, RUNS } from './keyboard-walkthrough/mock-api.mjs';

const output = path.resolve(process.argv[2] ?? 'screenshots/sessions-shell');
mkdirSync(output, { recursive: true });
const profile = mkdtempSync(path.join(tmpdir(), 'sessions-shell-'));
const { server, url } = await startMockApi(fileURLToPath(new URL('../dist/frontend/browser', import.meta.url)));
const browser = spawn(process.env.CHROME_BIN ?? 'google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--no-proxy-server', '--password-store=basic',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'
], { stdio: ['ignore', 'ignore', 'pipe'] });
let browserLog = '';
browser.stderr.on('data', data => { browserLog += data; });
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const pending = new Map();
let socket;
let nextId = 1;
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  const deadline = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
  pending.set(id, { resolve, reject, deadline });
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text);
  return response.result.value;
};

try {
  for (let attempt = 0; attempt < 50 && !socket; attempt++) {
    try {
      const port = readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
      const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = pages.find(candidate => candidate.type === 'page');
      if (page) {
        socket = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
      }
    } catch { await wait(100); }
  }
  if (!socket) throw new Error('Chrome did not expose a local debugging endpoint.');
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.deadline);
    if (message.error) request.reject(new Error(message.error.message));
    else request.resolve(message.result);
  };
  await send('Page.enable');
  mock.streamImage = await evaluate(`(() => {
    const canvas = document.createElement('canvas'); canvas.width = 240; canvas.height = 480;
    const context = canvas.getContext('2d'); context.fillStyle = '#f4f4f2'; context.fillRect(0, 0, 240, 480);
    context.fillStyle = '#162431'; context.fillRect(0, 0, 240, 60); context.fillStyle = '#ffffff';
    context.font = '18px sans-serif'; context.fillText('Synthetic phone', 20, 38);
    context.fillStyle = '#162431'; context.font = '16px sans-serif'; context.fillText('Login check running', 20, 130);
    context.fillText('No device was accessed', 20, 180); return canvas.toDataURL('image/png').split(',')[1];
  })()`);
  mock.readiness = { os_type: 'linux', overall_ready: true, probes: [{ id: 'system_config', status: 'pass' }, { id: 'llm_api_key', status: 'pass', metadata: { is_set: true } }] };
  mock.streamState = { connected: true, serial: 'emulator-5554', live_stream_url: '/api/stream/device-live' };
  const run = RUNS[0];
  run.chat_session_id = run.session_id; run.chat_title = 'Login checks'; run.chat_prompt = run.prompt;
  run.status = 'running'; run.end_time = null;
  mock.chat = { session_id: run.session_id, title: 'Login checks', lease_pid: null, idle_until: null,
    turns: [{ turn_id: run.session_id, number: 1, status: 'running', created_at: run.start_time }],
    messages: [{ sequence: 1, turn_id: run.session_id, role: 'human', content: 'Check that login works.', acknowledged_at: run.start_time },
      { sequence: 2, turn_id: run.session_id, role: 'agent', content: 'Checking the login screen.' }] };
  const receipts = [];
  for (const width of [1440, 390]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: false });
    const capture = async name => {
      await evaluate('document.fonts.ready');
      const issues = await evaluate(`(() => {
        const issues = document.documentElement.scrollWidth > innerWidth ? ['Horizontal overflow'] : [];
        for (const element of document.querySelectorAll('app-run-chat button, app-session-sidebar button, app-nav-switcher a, .new-run, .composer button')) {
          if (!element.checkVisibility()) continue;
          const box = element.getBoundingClientRect(); if (box.width < 44 || box.height < 44) issues.push(element.getAttribute('aria-label') || element.textContent.trim());
        } return issues;
      })()`);
      const screenshot = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(output, `${name}-${width}.png`), Buffer.from(screenshot.data, 'base64'));
      receipts.push({ name, width, issues });
    };
    await send('Page.navigate', { url: url + '/sessions/new' }); await wait(1200);
    await capture('empty');
    await send('Page.navigate', { url: url + '/sessions/' + run.session_id }); await wait(1400);
    if (!await evaluate("!!document.querySelector('app-session-sidebar .device-feed')")) throw new Error('Bound-phone Live fixture is missing.');
    await capture('running');
    for (const mode of ['expanded', 'collapsed', 'hover']) {
      await evaluate(`(() => { const select = document.querySelector('[aria-label="Navigation mode"]'); select.value = '${mode}'; select.dispatchEvent(new Event('change', { bubbles: true })); })()`);
      await wait(100);
      await capture(`nav-${mode}`);
    }
    if (width === 1440) {
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 25, y: 80 }); await wait(100);
      await capture('nav-hover-open');
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 900, y: 80 });
    }
    await evaluate("document.querySelector('[aria-controls=\"session-sidebar\"]').click()"); await wait(100);
    await capture('sidebar-collapsed');
  }
  writeFileSync(path.join(output, 'receipt.json'), JSON.stringify({ synthetic: true, device_accessed: false, captures: receipts }, null, 2));
  const failures = receipts.filter(receipt => receipt.issues.length);
  if (failures.length) throw new Error(JSON.stringify(failures));
  console.log(`PASS: ${receipts.length} synthetic screenshots; 44 px controls and no horizontal overflow.`);
} finally {
  socket?.close();
  for (const request of pending.values()) clearTimeout(request.deadline);
  if (browser.exitCode === null) {
    const exited = new Promise(resolve => browser.once('exit', resolve)); browser.kill(); await exited;
  }
  await new Promise(resolve => server.close(resolve));
  writeFileSync(path.join(output, 'chrome.log'), browserLog);
  rmSync(profile, { recursive: true, force: true });
}
