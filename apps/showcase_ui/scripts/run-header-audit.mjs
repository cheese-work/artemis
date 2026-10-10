import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const dist = path.resolve('dist/frontend/browser');
const shots = path.resolve(process.env.SHOTS || 'run-header-evidence');
const runId = '00000001-5d7e-4a10-9c33-0e1f2a3b4c5d';
const started = Math.floor(Date.now() / 1000) - 65;
const run = {
  session_id: runId, prompt: '# Open Settings and check the app\n\nVerify that Settings opens without an error.',
  status: 'completed', interrupt_reason: null, start_time: started, end_time: started + 60,
  host_id: null, device_ref: { host_id: null, serial: 'emulator-5554' },
  requested_by: 'qa@example.test', pinned: false, recordings: []
};
const steps = [{
  session_id: runId, step_id: 'step-1', step_number: 1, timestamp: started + 1,
  action_taken: { action: 'launch_app', package_name: 'com.android.settings' }
}];
const json = (response, body) => {
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end(JSON.stringify(body));
};
const server = createServer((request, response) => {
  if (process.env.DEBUG_AUDIT) console.log('HTTP', request.url);
  const pathname = new URL(request.url, 'http://localhost').pathname;
  if (pathname === '/api/runs') return json(response, { runs: [run], next_cursor: null, warnings: [] });
  if (pathname === `/api/runs/${runId}`) return json(response, run);
  if (pathname.endsWith('/steps')) return json(response, steps);
  if (pathname.endsWith('/notes')) return json(response, { notes: { 'output.md': 'Settings opened. The app is ready for the next task.' } });
  if (pathname.endsWith('/checks')) return json(response, { records: [], streams: [], run_outcome: null });
  if (pathname.endsWith('/video')) return json(response, { session_id: runId, status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
  if (pathname === '/api/system/whoami') return json(response, { email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
  if (pathname === '/api/sessions') return json(response, [{ session_id: runId, initial_goal: run.prompt, start_time: started, end_time: run.end_time, status: run.status, model_info: { name: 'Flash', id: 'fixture-flash', provider: 'fixture' } }]);
  if (pathname === '/api/hosts') return json(response, { enabled: true, hosts: [], devices: [] });
  if (pathname.startsWith('/api/')) return json(response, {});
  const extension = path.extname(pathname);
  const file = path.join(dist, extension ? pathname : 'index.html');
  try {
    response.writeHead(200, { 'content-type': { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' }[extension] ?? 'text/html' });
    response.end(readFileSync(file));
  } catch {
    response.writeHead(404);
    response.end();
  }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const base = `http://127.0.0.1:${server.address().port}`;
assert.equal((await fetch(base)).status, 200, 'fixture server responds');
const profile = mkdtempSync(path.join(tmpdir(), 'run-header-audit-'));
const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', [
  '--headless=new', '--no-sandbox', '--password-store=basic', '--no-proxy-server', '--disable-gpu', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank'
], { stdio: ['ignore', 'ignore', 'pipe'] });
let socket;
const pending = new Map();
let sequence = 0;
const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++sequence;
  const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
  pending.set(id, { resolve, reject, timeout });
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description ?? response.exceptionDetails.text);
  return response.result.value;
};
const results = [];

try {
  const browserUrl = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Chrome did not become ready')), 15000);
    chrome.once('error', reject);
    chrome.once('exit', () => { clearTimeout(timeout); reject(new Error('Chrome exited before becoming ready')); });
    chrome.stderr.on('data', data => {
      const match = data.toString().match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) { clearTimeout(timeout); resolve(match[1]); }
    });
  });
  const endpoint = new URL(browserUrl);
  const targets = await (await fetch(`http://${endpoint.host}/json/list`)).json();
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await once(socket, 'open');
  socket.addEventListener('message', event => {
    const response = JSON.parse(event.data);
    if (process.env.DEBUG_AUDIT && response.method?.startsWith('Network.')) console.log(response.method, response.params?.request?.url ?? response.params?.errorText ?? '');
    const request = pending.get(response.id);
    if (!request) return;
    pending.delete(response.id);
    clearTimeout(request.timeout);
    if (response.error) request.reject(new Error(response.error.message));
    else request.resolve(response.result);
  });
  socket.addEventListener('close', () => {
    for (const request of pending.values()) { clearTimeout(request.timeout); request.reject(new Error('Chrome connection closed')); }
    pending.clear();
  });
  await send('Page.enable');
  await send('Network.enable');
  if (process.env.DEBUG_AUDIT) console.log('Chrome ready', base);
  mkdirSync(shots, { recursive: true });
  for (const state of ['completed', 'running']) {
    run.status = state;
    run.end_time = state === 'running' ? null : started + 60;
    for (const width of [1440, 390]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: width < 800 });
      await send('Page.navigate', { url: `${base}/runs/${runId}` });
      let ready = false;
      for (let attempt = 0; attempt < 100; attempt++) {
        ready = await evaluate(`!!document.querySelector('.run-header')`).catch(() => false);
        if (ready) break;
        await wait(100);
      }
      assert.ok(ready, `${state} @${width}: header loads`);
      await wait(250);
      await evaluate('document.fonts.ready.then(() => true)');
      const snapshot = await evaluate(`(() => {
        const header = document.querySelector('.run-header');
        const primary = header.querySelector('.primary-actions button');
        const more = header.querySelector('.more-actions');
        const menuClosed = !more.open;
        more.open = true;
        const controls = [...header.querySelectorAll('button, summary, a')].filter(control => control.checkVisibility());
        const targets = controls.map(control => {
          const bounds = control.getBoundingClientRect();
          return { label: control.getAttribute('aria-label') || control.textContent.trim(), width: bounds.width, height: bounds.height };
        });
        const menu = [...more.querySelectorAll('button')].map(control => control.textContent.trim());
        more.open = false;
        return { title: header.querySelector('h1').textContent, primary: primary?.getAttribute('aria-label'), menuClosed, menu, targets,
          active: !!document.querySelector('.status-strip'), verdict: !!document.querySelector('.verdict-slot'),
          app: header.querySelector('[data-fact="app"]').textContent,
          model: header.querySelector('[data-fact="model"]').textContent,
          overflow: document.documentElement.scrollWidth - innerWidth,
          headerOverflow: header.scrollWidth - header.clientWidth,
          outside: [...header.querySelectorAll('*')].filter(element => element.checkVisibility() && element.getBoundingClientRect().right > header.getBoundingClientRect().right + 1).map(element => ({ tag: element.tagName, class: element.className, right: element.getBoundingClientRect().right, headerRight: header.getBoundingClientRect().right })) };
      })()`);
      if (snapshot.headerOverflow > 0) {
        console.log(JSON.stringify(snapshot, null, 2));
        const screenshot = await send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(path.join(shots, `failure-${state}-${width}.png`), Buffer.from(screenshot.data, 'base64'));
      }
      assert.equal(snapshot.title, 'Open Settings and check the app');
      assert.equal(snapshot.primary, state === 'running' ? 'Stop run' : 'Run again');
      assert.equal(snapshot.active, state === 'running');
      assert.equal(snapshot.verdict, state === 'completed');
      assert.equal(snapshot.app, 'com.android.settings');
      assert.equal(snapshot.model, 'Flash');
      assert.equal(snapshot.menuClosed, true);
      assert.deepEqual(snapshot.menu, ['Copy link', 'Download', 'Pin', 'Delete']);
      assert.ok(snapshot.overflow <= 0, `page overflow: ${snapshot.overflow}`);
      assert.ok(snapshot.headerOverflow <= 0, `header overflow: ${snapshot.headerOverflow}`);
      for (const control of snapshot.targets) {
        assert.ok(control.width >= 44 && control.height >= 44, JSON.stringify(control));
      }
      await evaluate(`Object.defineProperty(navigator.clipboard, 'writeText', { configurable: true, value: async text => { window.copiedRunId = text; } }); document.querySelector('.run-id-copy-button').click();`);
      await wait(50);
      assert.equal(await evaluate('window.copiedRunId'), runId);
      await evaluate(`document.querySelector('.full-prompt summary').click()`);
      assert.equal(await evaluate(`document.querySelector('.full-prompt').open`), true);
      await evaluate(`document.querySelector('.full-prompt summary').click()`);
      await evaluate(`(() => {
        document.querySelector('.more-actions > summary').click();
        document.querySelector('.more-actions button').click();
      })()`);
      assert.equal(await evaluate(`document.querySelector('.trust-dialog').open`), true, 'share trust dialog opens');
      await evaluate(`document.querySelector('.dialog-cancel').click()`);
      assert.equal(await evaluate(`document.activeElement === document.querySelector('.more-actions > summary')`), true, 'dialog focus returns to visible More trigger');
      assert.equal(await evaluate(`document.querySelector('.more-actions').open`), false, 'More closes when an action is chosen');
      if (state === 'running') {
        const before = await evaluate(`document.querySelector('[data-fact="duration"]').textContent`);
        await wait(1200);
        const after = await evaluate(`document.querySelector('[data-fact="duration"]').textContent`);
        assert.notEqual(after, before, 'elapsed updates');
        await evaluate(`(() => {
          const view = ng.getComponent(document.querySelector('app-run-view'));
          view.agentService.stopTask = target => { window.stopTarget = target; };
          document.querySelector('[aria-label="Stop run"]').click();
        })()`);
        assert.equal(await evaluate('window.stopTarget'), runId, 'Stop targets the displayed run');
      }
      await wait(1700);
      const screenshot = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(shots, `${state}-${width}.png`), Buffer.from(screenshot.data, 'base64'));
      await evaluate(`(() => {
        const view = ng.getComponent(document.querySelector('app-run-view'));
        view.ownerScope.identity.set({ email: 'other@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
        ng.applyChanges(view);
      })()`);
      assert.deepEqual(await evaluate(`(() => {
        const view = ng.getComponent(document.querySelector('app-run-view'));
        window.stopTarget = null;
        view.agentService.stopTask = target => { window.stopTarget = target; };
        view.onAction({ id: 'stop', event: new Event('click') });
        return { stop: !!document.querySelector('[aria-label="Stop run"]'), pin: !!document.querySelector('.more-actions button[aria-pressed]'), target: window.stopTarget };
      })()`), { stop: false, pin: false, target: null }, 'non-owner cannot stop or pin');
      results.push({ state, width, ...snapshot });
      console.log(`PASS ${state} @${width}: controls, copy, facts, slot, 44px boxes, no overflow`);
    }
  }
  writeFileSync(path.join(shots, 'results.json'), JSON.stringify(results, null, 2));
} catch (error) {
  console.error(error);
  throw error;
} finally {
  if (chrome.exitCode === null && chrome.signalCode === null) {
    const exited = once(chrome, 'exit');
    await send('Browser.close').catch(() => chrome.kill('SIGTERM'));
    await exited;
  }
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
