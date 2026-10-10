import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

const dist = path.resolve('dist/frontend/browser');
const timeline = process.argv.includes('--timeline');
const live = process.argv.includes('--live');
const shots = path.resolve(process.env.SHOTS || (live ? 'live-run-evidence' : timeline ? 'step-timeline-evidence' : 'run-header-evidence'));
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
const fixtureSteps = [1, 2, 3].map(number => ({
  session_id: runId, step_id: `step-${number}`, step_number: number, timestamp: started + number * 10,
  duration: number < 3 ? 1.2 : undefined, action_taken: { action: 'tap', args: { text: 'Network and internet' } }
}));
let liveSteps = [];
if (timeline) {
  steps.push(...['tap', 'input_text', 'tap'].map((action, index) => ({
    session_id: runId, step_id: `step-${index + 2}`, step_number: index + 2, timestamp: started + (index + 2) * 10,
    duration: 1.2 + index, action_taken: { action, args: { text: index === 1 ? 'Settings' : 'Network and internet' } },
    last_execution_result: index === 2 ? { success: false, error: 'Target not found on the current screen.' } : { success: true }
  })));
}
const json = (response, body) => {
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end(JSON.stringify(body));
};
const server = createServer((request, response) => {
  if (process.env.DEBUG_AUDIT) console.log('HTTP', request.url);
  const pathname = new URL(request.url, 'http://localhost').pathname;
  if (pathname === '/whats-new.json') return json(response, []);
  if (pathname === '/api/runs') return json(response, { runs: [run], next_cursor: null, warnings: [] });
  if (pathname === `/api/runs/${runId}`) return json(response, run);
  if (pathname.endsWith('/steps')) return json(response, live ? liveSteps : steps);
  if (pathname.endsWith('/notes')) return json(response, { notes: { 'output.md': 'Settings opened. The app is ready for the next task.' } });
  if (pathname.endsWith('/checks')) return json(response, { records: [], streams: [], run_outcome: null });
  if (pathname.endsWith('/video')) return json(response, { session_id: runId, status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
  if (pathname === '/api/system/whoami') return json(response, { email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
  if (pathname === '/api/sessions') return json(response, [{ session_id: runId, initial_goal: run.prompt, start_time: started, end_time: run.end_time, status: run.status, model_info: { name: 'Flash', id: 'fixture-flash', provider: 'fixture' } }]);
  if (pathname === '/api/hosts') return json(response, { enabled: true, hosts: [], devices: [] });
  if (pathname === '/api/status') return json(response, { status: 'idle', active_tasks: [] });
  if (pathname === '/api/system/readiness') return json(response, { os_type: 'linux', overall_ready: true, probes: [] });
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
  for (const state of live ? ['preparing', 'running', 'retrying', 'paused', 'interrupted', 'finished'] : timeline ? ['failed'] : ['completed', 'running']) {
    run.status = state === 'finished' ? 'completed' : ['preparing', 'retrying'].includes(state) ? 'running' : state;
    run.end_time = ['completed', 'interrupted'].includes(run.status) ? started + 60 : null;
    run.interrupt_reason = state === 'interrupted' ? 'device_offline' : null;
    liveSteps = state === 'preparing' ? [] : fixtureSteps;
    for (const width of [1440, 390]) {
      await send('Emulation.setDeviceMetricsOverride', { width, height: 1000, deviceScaleFactor: 1, mobile: width < 800 });
      await send('Page.navigate', { url: live ? `${base}/workspace` : `${base}/runs/${runId}` });
      let ready = false;
      for (let attempt = 0; attempt < 100; attempt++) {
        ready = await evaluate(`!!document.querySelector('${live ? 'app-workspace' : '.run-header'}')`).catch(() => false);
        if (ready) break;
        await wait(100);
      }
      assert.ok(ready, `${state} @${width}: header loads`);
      await wait(250);
      if (live) {
        await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
        await evaluate(`(() => {
          const workspace = ng.getComponent(document.querySelector('app-workspace'));
          const agent = workspace.agentService;
          const state = ${JSON.stringify(state)};
          const session = { session_id: '${runId}', initial_goal: ${JSON.stringify(run.prompt)}, start_time: ${started}, end_time: ${run.end_time},
            status: '${run.status}', interrupt_reason: ${JSON.stringify(run.interrupt_reason)}, device_serial: 'emulator-5554' };
          agent.rawSessions.set([session, { session_id: 'queued-fixture', initial_goal: 'Next task', status: 'pending', start_time: ${started + 60} }]);
          agent.currentSessionId.set(session.session_id);
          agent.runningSessionId.set(['preparing', 'running', 'retrying', 'paused'].includes(state) ? session.session_id : null);
          agent.agentStatus.set(state === 'paused' ? 'paused' : ['interrupted', 'finished'].includes(state) ? 'idle' : 'running');
          agent.isPaused.set(state === 'paused');
          agent.pausedError.set('The AI call failed. Continue when the service is available.');
          agent.isRetrying.set(state === 'retrying');
          agent.retryMessage.set('The AI service is temporarily busy. Attempt 2 of 3.');
          agent.startupProgressBySession.set({ [session.session_id]: [
            { stage: 'device_check', message: 'Checking the Android device', timestamp: ${started} },
            { stage: 'device_ready', message: 'Android device connected', timestamp: ${started + 0.8} },
            { stage: 'uiautomator', message: 'Connecting to the UI hierarchy service', timestamp: ${started + 1} }
          ] });
          agent.sessionLogs.set(state === 'preparing' ? [] : [{ type: 'llm_stream', session_id: session.session_id, timestamp: ${started + 30}, data: {
            execution_id: 'fixture-live', step_id: 'step-3', stream_type: 'thinking', text: 'Settings is open. I will check Network and internet, then verify that the expected controls are present.\\nThe screen matches the task.\\nI will keep the existing evidence available.\\nThis fourth line stays in the full live log.'
          } }]);
          workspace.phone.system.selectedRunTarget.set(state === 'interrupted' ? null : 'emulator-5554');
          workspace.phone.system.readinessReport.set({ os_type: 'linux', overall_ready: true, probes: [{ id: 'android_adb', status: 'pass', metadata: {
            devices: state === 'interrupted' ? [] : [{ serial: 'emulator-5554', state: 'device', model: 'Pixel fixture', device_kind: 'emulator', is_emulator: true }]
          } }] });
          ng.applyChanges(workspace);
        })()`);
        for (let attempt = 0; attempt < 100; attempt++) {
          if (await evaluate(`!!document.querySelector('.run-header')`).catch(() => false)) break;
          await wait(50);
        }
        await wait(150);
        const snapshot = await evaluate(`(() => {
          const view = ng.getComponent(document.querySelector('app-run-view'));
          const bounds = element => { const box = element.getBoundingClientRect(); return { width: box.width, height: box.height }; };
          return { strip: document.querySelector('.status-strip')?.textContent, current: !!document.querySelector('.current-step'),
            checklist: [...document.querySelectorAll('.startup-item')].map(bounds),
            announcement: document.querySelector('.run-status-slot').textContent.trim(),
            polite: document.querySelectorAll('app-run-view [aria-live="polite"]').length,
            stop: document.querySelectorAll('[aria-label="Stop run"]').length,
            composerStop: !!document.querySelector('.composer [aria-label="Stop run"]'),
            placeholder: document.querySelector('.composer-input').placeholder, hint: document.querySelector('.composer-hint').textContent,
            paused: !!document.querySelector('.status-strip .action-button'),
            banner: document.querySelector('.composer app-interrupted-banner .banner')?.textContent,
            duplicateBanner: !!document.querySelector('app-run-view .interrupted-banner'),
            thought: document.querySelector('.live-thought')?.textContent,
            clamp: document.querySelector('.live-thought') ? getComputedStyle(document.querySelector('.live-thought')).webkitLineClamp : null,
            animations: [...document.querySelectorAll('.status-strip .material-symbols-outlined, .current-step .step-icon, .startup-item .material-symbols-outlined')].map(element => getComputedStyle(element).animationName),
            dialogs: document.querySelectorAll('dialog[open]').length,
            controls: [...document.querySelectorAll('.follow-latest, .show-live-log, .status-strip button, .composer app-interrupted-banner button')].map(bounds),
            overflow: document.documentElement.scrollWidth - innerWidth };
        })()`);
        const active = !['finished', 'interrupted'].includes(state);
        assert.equal(snapshot.stop, active ? 1 : 0, 'one Stop in the run header');
        assert.equal(snapshot.composerStop, false);
        assert.equal(snapshot.polite, 1, 'only the run status strip announces');
        assert.equal(snapshot.dialogs, 0, 'screenshots show the run, not an overlay');
        assert.ok(snapshot.overflow <= 0, JSON.stringify(snapshot));
        assert.ok(snapshot.controls.every(box => box.width >= 44 && box.height >= 44), JSON.stringify(snapshot.controls));
        assert.ok(snapshot.animations.every(name => name === 'none'), 'reduced motion');
        assert.equal(snapshot.paused, state === 'paused');
        if (active) {
          assert.equal(snapshot.placeholder, 'Describe the next task. It queues after this run.');
          assert.match(snapshot.hint, /Pixel fixture.*1 run already queued/);
        }
        if (state === 'preparing') {
          assert.equal(snapshot.checklist.length, 2);
          assert.ok(snapshot.checklist.every(box => box.height >= 44));
        } else if (active) {
          assert.equal(snapshot.clamp, '3');
          assert.match(snapshot.thought, /Settings is open/);
          assert.equal(snapshot.current, true);
        }
        if (state === 'retrying') assert.match(snapshot.strip, /Retrying.*Attempt 2 of 3/s);
        if (state === 'paused') assert.match(snapshot.strip, /Task paused.*Continue task/s);
        if (state === 'interrupted') {
          assert.match(snapshot.banner, /phone went offline/);
          assert.equal(snapshot.duplicateBanner, false);
        }
        if (state === 'finished') {
          assert.equal(snapshot.announcement, 'Run finished: Pass.');
          assert.equal(snapshot.current, false);
        }
        if (width === 390) {
          await evaluate(`(document.querySelector('.status-strip') ?? document.querySelector('.run-result-summary')).scrollIntoView({ block: 'start', behavior: 'instant' })`);
          await wait(50);
        }
        const screenshot = await send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(path.join(shots, `${state}-${width}.png`), Buffer.from(screenshot.data, 'base64'));
        if (state === 'running') {
          await evaluate(`(() => {
            window.announcements = [];
            const strip = document.querySelector('.run-status-slot');
            window.announcementObserver = new MutationObserver(() => window.announcements.push(strip.textContent.trim()));
            window.announcementObserver.observe(strip, { subtree: true, childList: true, characterData: true });
            document.querySelector('.step-button').click();
          })()`);
          await wait(50);
          assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).followLatest()`), false);
          await evaluate(`(() => {
            const view = ng.getComponent(document.querySelector('app-run-view'));
            view.storedSteps.update(steps => [...steps, { session_id: '${runId}', step_id: 'step-4', step_number: 4, timestamp: Date.now() / 1000, action_taken: { action: 'tap' } }]);
          })()`);
          await wait(50);
          assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).selectedStep().step_number`), 1, 'manual evidence stays selected');
          await evaluate(`document.querySelector('.follow-latest').click()`);
          await wait(50);
          assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).selectedStep().step_number`), 4, 'Follow latest resumes');
          await evaluate(`(() => {
            const view = ng.getComponent(document.querySelector('app-run-view'));
            view.clock.set(Date.now() / 1000 + 1);
            view.agentService.sessionLogs.set([{ type: 'llm_stream', timestamp: Date.now() / 1000, data: { execution_id: 'four', step_id: 'step-4', stream_type: 'thinking', text: 'A new thought' } }]);
            view.agentService.isRetrying.set(true);
            view.agentService.retryMessage.set('Retrying in 2s');
          })()`);
          await wait(50);
          assert.deepEqual(await evaluate(`window.announcements`), ['Step 4 in progress.'], 'clock, selection, Thought and retry do not announce');
          await evaluate(`(() => {
            const view = ng.getComponent(document.querySelector('app-run-view'));
            view.agentService.runningSessionId.set(null);
            view.agentService.agentStatus.set('idle');
            view.agentService.rawSessions.update(sessions => sessions.map(session => session.session_id === '${runId}' ? { ...session, status: 'failed', end_time: Date.now() / 1000 } : session));
          })()`);
          await wait(100);
          assert.deepEqual(await evaluate(`window.announcements`), ['Step 4 in progress.', 'Run finished: Fail.'], 'one terminal announcement');
          await evaluate(`window.announcementObserver.disconnect()`);
          snapshot.behavior = 'PASS: manual selection, follow toggle and announcement count';
        }
        results.push({ state, width, ...snapshot });
        console.log(`PASS ${state} @${width}: strip, controls, composer, reduced motion, no overflow`);
        continue;
      }
      await evaluate('document.fonts.ready.then(() => true)');
      if (timeline) {
        await evaluate(`(() => {
          const canvas = document.createElement('canvas');
          canvas.width = 180; canvas.height = 240;
          const context = canvas.getContext('2d');
          context.fillStyle = '#fafafa'; context.fillRect(0, 0, 180, 240);
          context.fillStyle = '#27272a'; context.font = '18px sans-serif'; context.fillText('Settings', 16, 42);
          context.font = '11px sans-serif';
          ['Network and internet', 'Connected devices', 'Apps', 'Notifications', 'Battery'].forEach((label, index) => context.fillText(label, 16, 82 + index * 30));
          const view = ng.getComponent(document.querySelector('app-run-view'));
          view.storedSteps.set(view.steps().map(step => ({ ...step, pre_image_name: canvas.toDataURL(), post_image_name: canvas.toDataURL() })));
          window.timelineSteps = view.steps();
          ng.applyChanges(view);
        })()`);
        await wait(100);
        const snapshot = await evaluate(`(() => {
          const bounds = element => { const box = element.getBoundingClientRect(); return { width: box.width, height: box.height }; };
          const rows = [...document.querySelectorAll('.step-button')];
          const failed = rows.at(-1);
          return { rows: rows.map(bounds), thumbnails: rows.map(row => bounds(row.querySelector('.step-thumbnail'))),
            controls: [...document.querySelectorAll('.step-toggle')].map(bounds),
            label: failed.querySelector('.step-failed').textContent, icon: failed.querySelector('.step-icon').textContent,
            numberSize: getComputedStyle(failed.querySelector('.step-number')).fontSize,
            failureSize: getComputedStyle(failed.querySelector('.step-failed')).fontSize,
            failureDisplay: getComputedStyle(failed.querySelector('.step-failed')).display,
            background: getComputedStyle(failed.closest('.step-row')).backgroundColor,
            kind: rows[1].querySelector('.step-kind').textContent,
            time: rows[1].querySelector('.step-duration').textContent,
            durationSize: getComputedStyle(rows[1].querySelector('.step-duration')).fontSize,
            mono: getComputedStyle(rows[1].querySelector('.step-duration')).fontFamily,
            screenshotsCollapsed: !document.querySelector('.step-screenshots'),
            overflow: document.documentElement.scrollWidth - innerWidth,
            timelineOverflow: document.querySelector('.steps').scrollWidth - document.querySelector('.steps').clientWidth };
        })()`);
        assert.equal(snapshot.rows.length, 4);
        assert.ok(snapshot.rows.every(box => box.height === 56 && box.width >= 44), JSON.stringify(snapshot.rows));
        assert.ok(snapshot.thumbnails.every(box => box.width === 56 && box.height === 40));
        assert.ok(snapshot.controls.every(box => box.width >= 44 && box.height >= 44));
        assert.equal(snapshot.label, 'Failed');
        assert.equal(snapshot.icon, 'error');
        assert.equal(snapshot.numberSize, '12px');
        assert.equal(snapshot.failureSize, '12px');
        assert.equal(snapshot.failureDisplay, 'flex');
        assert.equal(snapshot.background, 'rgb(254, 226, 226)');
        assert.equal(snapshot.kind, 'tap');
        assert.equal(snapshot.time, '1.2s');
        assert.equal(snapshot.durationSize, '12px');
        assert.match(snapshot.mono, /mono/i);
        assert.equal(snapshot.screenshotsCollapsed, true);
        assert.ok(snapshot.overflow <= 0 && snapshot.timelineOverflow <= 0, JSON.stringify(snapshot));
        await evaluate(`document.querySelector('.step-button').click(); document.querySelector('.step-button').focus()`);
        await wait(50);
        assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).selectedStep().step_number`), 1);
        for (const [key, expected] of [['ArrowUp', 1], ['ArrowDown', 2], ['End', 4], ['ArrowDown', 4], ['Home', 1]]) {
          await send('Input.dispatchKeyEvent', { type: 'keyDown', key, code: key });
          await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: key });
          await wait(50);
          assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).selectedStep().step_number`), expected, key);
        }
        await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
        assert.equal(await evaluate(`document.activeElement === document.querySelector('.step-toggle')`), true, 'native Tab reaches details');
        await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
        await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32 });
        await wait(50);
        assert.equal(await evaluate(`!!document.querySelector('#step-details-step-1') && document.querySelector('.step-toggle').getAttribute('aria-expanded') === 'true'`), true);
        assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).selectedStep().step_number`), 1, 'details do not select');
        await evaluate(`document.querySelector('.step-toggle').click()`);
        await wait(50);
        assert.equal(await evaluate(`!!document.querySelector('#step-details-step-1')`), false);
        assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).goToStep(4)`), true);
        await wait(100);
        assert.equal(await evaluate(`document.activeElement === document.querySelectorAll('.step-button')[3] && document.querySelectorAll('.step-button')[3].getAttribute('aria-current') === 'step' && document.querySelector('.evidence-image').getAttribute('alt') === 'Screenshot for step 4'`), true);
        assert.equal(await evaluate(`ng.getComponent(document.querySelector('app-run-view')).goToStep(999)`), false);
        assert.equal(await evaluate(`getComputedStyle(document.querySelectorAll('.step-button')[3]).outlineWidth`), '3px', 'keyboard focus outline remains');
        await evaluate(`document.querySelectorAll('.step-button')[3].blur()`);
        snapshot.selectedFailedShadow = await evaluate(`getComputedStyle(document.querySelectorAll('.step-button')[3]).boxShadow`);
        assert.match(snapshot.selectedFailedShadow, /inset/, 'selected failed row keeps an inset marker after blur');
        await evaluate(`ng.getComponent(document.querySelector('app-run-view')).goToStep(1)`);
        await wait(50);
        await evaluate(`document.querySelector('.step-button').blur()`);
        snapshot.unselectedFailedShadow = await evaluate(`getComputedStyle(document.querySelectorAll('.step-button')[3]).boxShadow`);
        assert.equal(snapshot.unselectedFailedShadow, 'none');
        assert.notEqual(snapshot.selectedFailedShadow, snapshot.unselectedFailedShadow, 'failed-row selection is distinct without focus');
        await evaluate(`ng.getComponent(document.querySelector('app-run-view')).goToStep(4)`);
        await wait(50);
        await evaluate(`document.querySelectorAll('.step-button')[3].blur()`);
        await evaluate(`(() => {
          const view = ng.getComponent(document.querySelector('app-run-view'));
          view.storedSteps.set(window.timelineSteps.map((step, index) => index === 0
            ? { ...step, last_execution_result: { success: false, error: '# Failure report' + String.fromCharCode(10) + 'Details '.repeat(300) } } : step));
          ng.applyChanges(view);
        })()`);
        assert.equal(await evaluate(`document.querySelector('.step-failure-detail').hasAttribute('title')`), false, 'long reports do not create oversized tooltips');
        await evaluate(`(() => {
          const view = ng.getComponent(document.querySelector('app-run-view'));
          view.storedSteps.set(window.timelineSteps);
          ng.applyChanges(view);
        })()`);
        await evaluate(`document.querySelector('.steps').scrollIntoView({ block: 'end' })`);
        const screenshot = await send('Page.captureScreenshot', { format: 'png' });
        writeFileSync(path.join(shots, `failed-${width}.png`), Buffer.from(screenshot.data, 'base64'));
        if (width === 390) {
          await send('Emulation.setDeviceMetricsOverride', { width, height: 1800, deviceScaleFactor: 1, mobile: true });
          await evaluate(`document.querySelector('.viewer-scroll').scrollTop = 0`);
          await wait(100);
          const overview = await send('Page.captureScreenshot', { format: 'png' });
          writeFileSync(path.join(shots, 'finished-failed-390.png'), Buffer.from(overview.data, 'base64'));
        }
        results.push({ state, width, ...snapshot });
        console.log(`PASS step timeline @${width}: selection marker after blur, keyboard focus, verdict jump, details, 56px rows, 56x40 thumbnails, 44px toggles, no overflow`);
        continue;
      }
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
