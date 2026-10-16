import http from 'node:http';
import { spawn } from 'node:child_process';
import { readFileSync, mkdirSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import path from 'node:path';

const [dist, label, outputDirectory = 'docs/assets/che-1459'] = process.argv.slice(2);
const output = path.resolve(outputDirectory);
mkdirSync(output, { recursive: true });
const id = '11111111-2222-4333-8444-555555555555';
const start = 1791529200;
const steps = [
  { step_id: 'st1', step_number: 1, session_id: id, timestamp: start + 10,
    action_taken: { action: 'tap', args: { text: 'Settings', x: 40, y: 80 }, status: 'success' },
    operator_native_thinking: 'Find the Settings entry and open the screen.',
    operator_raw_thinking: 'Opening Settings to verify the dark mode toggle.',
    pre_image_name: 'before.png', post_image_name: 'after.png', duration: 1.2,
    token_usage: { total_tokens: 250 }, last_execution_result: { status: 'success', message: 'Settings opened' },
    generic_tools: [{ name: 'read_note', args: { name: 'plan.md' }, result: 'Verify the Settings title and dark mode toggle.', timestamp: start + 11 }] },
  { step_id: 'st2', step_number: 2, session_id: id, timestamp: start + 20,
    action_taken: { action: 'report_task_status', args: { status: 'completed', explanation: 'Settings opened. Dark mode is available.' } },
    pre_image_name: 'after.png', last_execution_result: { status: 'success' } }
];
const checks = {
  records: [{ attempt_id: 'check1', checkpoint_id: 'final', anchor_step_id: 'st1', ts: start + 25,
    item_text: 'Settings title and dark mode are visible', kind: 'assert', status: 'passed', evidence: 'The Settings screen contains the dark mode toggle.' }],
  streams: [{ attempt_id: 'check1', segments: [{ execution_id: 'checker-turn', role: 'thought', when: start + 24, text: 'Inspect the Settings title and toggle labels.' }] }],
  run_outcome: { task_status: 'completed', tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 }, last_findings: ['No unmet subgoals.'] }
};
let live = false;
const notes = { notes: { 'output.md': '# Settings verification\nOpened Settings and verified the dark mode toggle.\n\n- [x] Settings title is visible\n- [x] Dark mode toggle is available', 'plan.md': '# Plan\n- [x] Open Settings\n- [x] Verify dark mode' } };
const json = (response, body) => { response.writeHead(200, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(body)); };
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, 'http://local').pathname;
  const session = { session_id: id, initial_goal: 'Open Settings and verify dark mode', prompt: 'Open Settings and verify dark mode', status: live ? 'running' : 'completed', start_time: start, end_time: live ? null : start + 60, host_id: null, device_serial: 'fixture-phone', device_ref: { host_id: null, serial: 'fixture-phone' }, recordings: [], pinned: false, interrupt_reason: null, requested_by: 'qa@example.test' };
  if (pathname === '/api/system/whoami') return json(response, { email: 'qa@example.test', admin: true, auth_mode: 'cloudflare' });
  if (pathname === '/api/system/readiness') return json(response, { overall_ready: true, os_type: 'linux', probes: [{ id: 'system_config', status: 'pass' }, { id: 'llm_api_key', status: 'pass', metadata: { is_set: true } }, { id: 'android_adb', status: 'pass', metadata: { devices: [] } }] });
  if (pathname === '/api/sessions') return json(response, [session]);
  if (pathname === '/api/runs') return json(response, { runs: [session], warnings: [], next_cursor: null });
  if (pathname === `/api/runs/${id}`) return json(response, session);
  if (pathname.endsWith('/steps')) return json(response, steps);
  if (pathname.endsWith('/notes')) return json(response, notes);
  if (pathname.endsWith('/checks')) return json(response, checks);
  if (pathname.endsWith('/startup_progress')) return json(response, []);
  if (pathname.endsWith('/usage')) return json(response, { session_id: id, total_tokens: 400, prompt_tokens: 250, completion_tokens: 150 });
  if (pathname.endsWith('/video')) return json(response, { session_id: id, status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
  if (pathname === '/api/status') return json(response, { status: live ? 'running' : 'idle', session_id: live ? id : null, current_session_id: live ? id : null, running_session_id: live ? id : null });
  if (pathname === '/api/hosts') return json(response, { enabled: false, hosts: [], devices: [] });
  if (pathname === '/api/devices') return json(response, { devices: [] });
  if (pathname.startsWith('/images/')) { response.writeHead(200, { 'Content-Type': 'image/png' }); return response.end(readFileSync(path.join(output, 'phone.png'))); }
  if (pathname.includes('/stream')) { response.writeHead(200, { 'Content-Type': 'text/event-stream' }); return response.end('event: connected\ndata: {}\n\n'); }
  if (pathname.startsWith('/api/')) return json(response, {});
  try { const extension = path.extname(pathname); const filename = extension ? pathname : '/index.html'; response.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json' })[extension || '.html'] || 'application/octet-stream' }); response.end(readFileSync(path.join(dist, filename))); }
  catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const profile = mkdtempSync('/tmp/che1459-chrome-');
const chrome = spawn('/usr/bin/google-chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--password-store=basic', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore', detached: true });
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let websocket;
let sequence = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => { const commandId = ++sequence; pending.set(commandId, { resolve, reject }); websocket.send(JSON.stringify({ id: commandId, method, params })); });
try {
  let port;
  for (let attempt = 0; attempt < 100; attempt++) { try { port = Number(readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]); break; } catch { await pause(100); } }
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === 'page');
  websocket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { websocket.onopen = resolve; websocket.onerror = reject; });
  websocket.onmessage = event => { const message = JSON.parse(event.data); if (message.method === 'Runtime.exceptionThrown' || (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error')) console.error(JSON.stringify(message.params)); if (message.id && pending.has(message.id)) { const request = pending.get(message.id); pending.delete(message.id); message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result); } };
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Accessibility.enable');
  const base = `http://127.0.0.1:${server.address().port}`;
  const evaluate = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true })).result.value;
  const receipts = [];
  for (const state of ['finished', 'live']) {
    live = state === 'live';
    for (const width of [1440, 390]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: false });
      await send('Page.navigate', { url: base + (label === 'old' || live ? '/workspace' : `/runs/${id}`) });
      await pause(2400);
      await evaluate("document.querySelector('[aria-label=\"Close What\\\'s New\"]')?.click()");
      await pause(300);
      if (label === 'after') {
        await evaluate("document.querySelector('.step-toggle')?.click()");
        await pause(200);
      }
      const receipt = await evaluate(`({ title: document.querySelector('.run-prompt')?.textContent, summary: document.querySelector('.run-result-summary')?.textContent, details: document.querySelector('.step-details')?.textContent, oldReport: document.querySelector('.outputter-synthesis-stream-card')?.textContent, overflow: document.documentElement.scrollWidth > innerWidth, toggles: [...document.querySelectorAll('.step-toggle')].map(button => ({ expanded: button.getAttribute('aria-expanded'), controls: button.getAttribute('aria-controls') })) })`);
      if (label === 'after') {
        await send('Page.bringToFront');
        await evaluate("document.querySelector('.step-toggle')?.focus()");
        await pause(300);
        receipt.focusBeforeKeyboard = await evaluate("({ name: document.activeElement?.getAttribute('aria-label'), expanded: document.activeElement?.getAttribute('aria-expanded') })");
        await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r' });
        await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
        await pause(500);
        receipt.keyboardCollapsed = await evaluate("document.querySelector('.step-toggle')?.getAttribute('aria-expanded') === 'false'");
        await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
        await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
        await pause(500);
        receipt.keyboardExpanded = await evaluate("document.querySelector('.step-toggle')?.getAttribute('aria-expanded') === 'true'");
        const tree = await send('Accessibility.getFullAXTree');
        const control = tree.nodes.find(node => node.role?.value === 'button' && node.name?.value === 'Step 1 details');
        receipt.accessibilityControl = control ? { role: control.role.value, name: control.name.value, expanded: control.properties.find(property => property.name === 'expanded')?.value.value } : null;
      }
      await evaluate("document.activeElement?.blur(); document.querySelectorAll('*').forEach(element => { if (element.scrollTop) element.scrollTop = 0; }); window.scrollTo(0, 0)");
      await pause(200);
      const { data } = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(output, `${label}-${state}-${width}.png`), Buffer.from(data, 'base64'));
      if (label === 'after') {
        await evaluate("document.querySelector('.step-details')?.scrollIntoView({ block: 'start' })");
        const detailShot = await send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(path.join(output, `${label}-${state}-${width}-details.png`), Buffer.from(detailShot.data, 'base64'));
      }
      if (label === 'old') {
        await evaluate("document.querySelector('.outputter-synthesis-stream-card')?.scrollIntoView({ block: 'center' })");
        const resultShot = await send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(path.join(output, `${label}-${state}-${width}-result.png`), Buffer.from(resultShot.data, 'base64'));
      }
      receipts.push({ state, width, ...receipt });
    }
  }
  writeFileSync(path.join(output, `${label}-browser.json`), JSON.stringify(receipts, null, 2));
  console.log(JSON.stringify(receipts, null, 2));
  if (label === 'after' && receipts.some(receipt => !receipt.summary?.includes('Passed: 1') || !receipt.details?.includes('Inspect the Settings title') || !receipt.keyboardCollapsed || !receipt.keyboardExpanded || !receipt.accessibilityControl?.expanded || receipt.overflow)) throw new Error('Run regression browser checks failed');
  if (label === 'before' && receipts.some(receipt => receipt.summary || receipt.toggles.length)) throw new Error('Baseline unexpectedly contains restored controls');
} finally {
  websocket?.close();
  try { process.kill(-chrome.pid, 'SIGTERM'); } catch {}
  if (chrome.exitCode === null) await new Promise(resolve => chrome.on('exit', resolve));
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}
