import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock, RUNS } from './keyboard-walkthrough/mock-api.mjs';

const dist = fileURLToPath(new URL('../dist/frontend/browser', import.meta.url));
const evidence = process.env.SHOTS || fileURLToPath(new URL('../run-list-evidence', import.meta.url));
assert.ok(existsSync(path.join(dist, 'index.html')), 'Build the development UI first');
mkdirSync(evidence, { recursive: true });
const today = new Date();
today.setHours(12, 0, 0, 0);
const yesterday = new Date(today);
yesterday.setDate(yesterday.getDate() - 1);
RUNS.forEach((run, index) => {
  run.start_time = (index < 3 ? today : yesterday).getTime() / 1000 - index * 300;
  run.app_package = 'com.example.shop';
});
const running = { ...RUNS[0], session_id: '11111111-live', prompt: '\n## **Check** _checkout_\nVerify the order summary', status: 'running' };
const queued = { ...RUNS[0], session_id: '22222222-queued', prompt: 'Confirm the order appears in history', status: 'pending' };
RUNS.unshift(running, queued);
mock.sessions = [running, queued].map((run) => ({ ...run, initial_goal: run.prompt, device_serial: 'emulator-5554' }));
mock.status = { status: 'running', session_id: running.session_id, goal: running.prompt, queue: [mock.sessions[1]] };
const { server, url: base } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'che-1499-chrome-'));
const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--password-store=basic',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'
], { stdio: 'ignore' });
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const waitFor = async (predicate) => {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await predicate()) return;
    await sleep(100);
  }
  throw new Error('Browser readiness timed out');
};
let socket;
let nextId = 0;
const pending = new Map();
const errors = [];
const receipt = [];
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  assert.ok(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
  return response.result.value;
};
const click = async (selector) => {
  await waitFor(() => evaluate(`(() => { const target = document.querySelector(${JSON.stringify(selector)}); if (!target) return false; const box = target.getBoundingClientRect(); return box.width > 0 && target.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)); })()`));
  const point = await evaluate(`(() => { const box = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
};
const key = async (value) => {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: value });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: value });
};
const timeout = setTimeout(() => { console.error('Run-list audit timed out'); chrome.kill(); server.close(); }, 110000);

try {
  await waitFor(() => existsSync(path.join(profile, 'DevToolsActivePort')));
  const port = readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
  const tabs = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
  socket = new WebSocket(tabs.find((tab) => tab.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    if (message.error) request.reject(new Error(JSON.stringify(message.error)));
    else request.resolve(message.result);
  };
  socket.onclose = () => {
    for (const request of pending.values()) request.reject(new Error('Browser connection closed'));
    pending.clear();
  };
  await send('Page.enable');
  await send('Runtime.enable');
  for (const width of [1440, 390]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 600 });
    await send('Page.navigate', { url: base + '/runs' });
    await waitFor(() => evaluate("!!window.ng && document.querySelectorAll('.queue-row').length === 2 && document.querySelectorAll('.date-group').length === 2"));
    await evaluate('document.fonts.ready');
    const layout = await evaluate(`(() => {
      const box = (element) => { const rect = element.getBoundingClientRect(); return { width: rect.width, height: rect.height, top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right }; };
      return {
        header: box(document.querySelector('.library-heading')),
        nav: box(document.querySelector('.floating-nav-switcher')),
        pane: box(document.querySelector('.run-list-pane')),
        controls: [...document.querySelectorAll('app-run-library [role=tab], app-run-library input[type=search], .new-run, .queue-stop, .queue-cancel, .filter-chips button')].map(box),
        chipVisuals: [...document.querySelectorAll('.filter-chips button > span')].map(box),
        chipGap: getComputedStyle(document.querySelector('.filter-chips')).gap,
        rows: [...document.querySelectorAll('app-run-library .run-row')].map(box),
        overflow: document.documentElement.scrollWidth - innerWidth,
        groups: [...document.querySelectorAll('.date-group-title')].map((heading) => heading.textContent.trim()),
        statuses: [...document.querySelectorAll('.run-outcome')].map((status) => status.textContent.trim())
      };
    })()`);
    assert.equal(layout.overflow, 0, `${width}px horizontal overflow`);
    assert.ok(layout.header.left >= layout.nav.right || layout.header.top >= layout.nav.bottom, `${width}px header under navigation`);
    if (width === 1440) assert.equal(layout.pane.width, 360);
    for (const box of layout.controls) assert.ok(box.width >= 44 && box.height >= 44, `${width}px control ${JSON.stringify(box)}`);
    for (const box of layout.chipVisuals) assert.equal(box.height, 32, `${width}px chip visual`);
    assert.equal(layout.chipGap, '8px');
    assert.equal(await evaluate("document.querySelector('app-run-library select, app-run-library input[type=date]') === null"), true);
    for (const box of layout.rows) assert.equal(box.height, 56, `${width}px row height`);
    assert.ok(layout.groups.includes('Today') && layout.groups.includes('Yesterday'));
    assert.ok(layout.statuses.includes('Completed') && !layout.statuses.includes('Passed'));
    const completedRows = await evaluate(`(() => [...document.querySelectorAll('app-run-library .run-row')]
      .filter((row) => row.querySelector('.run-outcome').textContent.trim() === 'Completed')
      .map((row) => ({
        tone: row.querySelector('.run-icon').className,
        icon: row.querySelector('.run-icon .material-symbols-outlined').textContent.trim(),
        packageGap: row.querySelector('.run-package').getBoundingClientRect().left - row.querySelector('.run-outcome').getBoundingClientRect().right
      })))()`);
    assert.ok(completedRows.length > 0);
    for (const row of completedRows) {
      assert.equal(row.tone, 'run-icon tone-neutral');
      assert.equal(row.icon, 'description');
      assert.ok(row.packageGap >= 4, `${width}px package separator gap`);
    }
    assert.equal(await evaluate("document.querySelector('.queue-row .run-prompt').textContent"), 'Check checkout');
    await evaluate("document.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true }))");
    assert.equal(await evaluate("document.activeElement === document.querySelector('input[type=search]')"), true);
    const typing = await evaluate("(() => { const event = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true }); document.activeElement.dispatchEvent(event); return event.defaultPrevented; })()");
    assert.equal(typing, false);
    await evaluate('document.activeElement.blur()');
    const screenshot = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(path.join(evidence, `run-list-${width}.png`), Buffer.from(screenshot.data, 'base64'));
    writeFileSync(path.join(evidence, `filter-chips-closed-${width}.png`), Buffer.from(screenshot.data, 'base64'));
    await click('[data-filter=app]');
    assert.equal(await evaluate("document.querySelector('[data-filter=app]').getAttribute('aria-disabled')"), 'true');
    assert.equal(await evaluate("document.querySelector('[data-filter=app]').title"), 'Needs backend support');
    assert.equal(await evaluate('location.search'), '');
    assert.equal(await evaluate("document.querySelector('.filter-popover') === null"), true);
    await click('[data-filter=status]');
    await waitFor(() => evaluate("document.activeElement?.getAttribute('data-filter-option') === ''"));
    const popover = await evaluate(`(() => {
      const box = document.querySelector('.filter-popover').getBoundingClientRect();
      return { left: box.left, right: box.right, top: box.top, bottom: box.bottom,
        targets: [...document.querySelectorAll('.filter-popover button')].map((button) => ({ width: button.getBoundingClientRect().width, height: button.getBoundingClientRect().height })) };
    })()`);
    assert.ok(popover.left >= 0 && popover.right <= width && popover.top >= 0 && popover.bottom <= 900, `${width}px popover bounds`);
    for (const box of popover.targets) assert.ok(box.width >= 44 && box.height >= 44, `${width}px popover target`);
    const openShot = await send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(path.join(evidence, `filter-chips-open-${width}.png`), Buffer.from(openShot.data, 'base64'));
    await key('ArrowDown');
    assert.equal(await evaluate("document.activeElement.getAttribute('data-filter-option')"), 'completed');
    await key('End');
    assert.equal(await evaluate("document.activeElement.getAttribute('data-filter-option')"), 'cancelled');
    await key('Tab');
    assert.equal(await evaluate("document.activeElement.closest('.filter-popover') !== null"), true);
    await key('Escape');
    await waitFor(() => evaluate("document.querySelector('.filter-popover') === null"));
    assert.equal(await evaluate("document.activeElement.getAttribute('data-filter')"), 'status');
    await click('[data-filter=status]');
    await waitFor(() => evaluate("document.querySelector('.filter-popover') !== null"));
    await waitFor(() => evaluate(`document.elementFromPoint(${width - 12}, 880)?.classList.contains('cdk-overlay-backdrop')`));
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: width - 12, y: 880, button: 'left', clickCount: 1 });
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: width - 12, y: 880, button: 'left', clickCount: 1 });
    await waitFor(() => evaluate("document.querySelector('.filter-popover') === null"));
    await click('[data-filter=date]');
    await waitFor(() => evaluate("document.querySelector('[data-filter-option=week]') !== null"));
    await click('[data-filter-option=week]');
    await waitFor(() => evaluate("location.search.includes('from=') && location.search.includes('to=') && document.querySelector('.filter-popover') === null"));
    assert.equal(await evaluate("document.querySelector('[data-filter=date]').textContent.includes('Last 7 days')"), true);
    await click('[aria-label="Clear Date filter"]');
    await waitFor(() => evaluate(`location.search === '' && document.querySelector('[aria-label="Clear Date filter"]') === null`));
    await click('[data-filter=add]');
    await waitFor(() => evaluate("document.querySelector('[data-add-filter=device]') !== null"));
    assert.equal(await evaluate(`(() => { const box = document.querySelector('.filter-popover').getBoundingClientRect(); return box.left >= 0 && box.right <= ${width} && box.top >= 0 && box.bottom <= 900; })()`), true, `${width}px + Filter popover bounds`);
    await click('[data-add-filter=device]');
    await waitFor(() => evaluate("document.activeElement?.getAttribute('aria-label') === 'Phone'"));
    await evaluate("(() => { const input = document.querySelector('.filter-popover input'); input.value = 'emulator-5554'; input.dispatchEvent(new Event('input', { bubbles: true })); })()");
    await click('.filter-popover button[type=submit]');
    await waitFor(() => evaluate("location.search.includes('device=emulator-5554') && document.querySelector('.filter-popover') === null"));
    await click('[aria-label="Clear Phone filter"]');
    await waitFor(() => evaluate(`location.search === '' && document.querySelector('[aria-label="Clear Phone filter"]') === null`));
    await evaluate(`(() => {
      const workspace = ng.getComponent(document.querySelector('app-workspace'));
      window.stopCalls = [];
      workspace.agentService.stopTask = (...args) => window.stopCalls.push(args);
      document.querySelector('.queue-stop').click();
      document.querySelector('.queue-cancel').click();
    })()`);
    assert.deepEqual(await evaluate('window.stopCalls'), [[running.session_id, false], [queued.session_id, false]]);
    await evaluate("document.querySelector('[data-scope=everyone]').click()");
    await waitFor(() => evaluate("ng.getComponent(document.querySelector('app-run-library')).scope() === 'everyone' && !ng.getComponent(document.querySelector('app-run-library')).loading()"));
    assert.equal(await evaluate("document.querySelector('[data-scope=everyone]').getAttribute('aria-selected')"), 'true');
    await send('Page.navigate', { url: base + '/workspace' });
    await waitFor(() => evaluate(`document.querySelectorAll('.queue-row').length === 2 &&
      ng.getComponent(document.querySelector('app-workspace')).agentService.currentSessionId() === ${JSON.stringify(running.session_id)}`));
    const selections = [];
    for (const sessionId of [queued.session_id, running.session_id]) {
      await evaluate(`([...document.querySelectorAll('.queue-row .run-row')]
        .find((row) => ng.getComponent(row).run().session_id === ${JSON.stringify(sessionId)})).click()`);
      await waitFor(() => evaluate("!ng.getComponent(document.querySelector('app-workspace')).router.currentNavigation()"));
      const selection = await evaluate(`(() => {
        const workspace = ng.getComponent(document.querySelector('app-workspace'));
        return { url: location.pathname, sessionId: workspace.agentService.currentSessionId(), review: workspace.reviewMode };
      })()`);
      assert.equal(selection.url, '/workspace', `${width}px Queue selection must not navigate`);
      assert.equal(selection.sessionId, sessionId);
      assert.equal(selection.review, false);
      selections.push(selection);
    }
    receipt.push({ width, result: 'PASS', ...layout, completedRows, popover, selections, checks: ['44px targets', '32px chips and 8px gap', 'unavailable App inert', 'popover bounds', 'native pointer and keyboard input', 'arrow keys and focus trapping', 'Escape and outside dismissal', 'focus restoration', 'date preset URL round-trip', '+ Filter text URL round-trip', '56px rows', 'date groups', 'status words', 'neutral completion icons', 'package separator gap', 'slash focus and editable guard', 'targeted Stop/Cancel', 'Everyone tab', 'in-place Workspace Queue selection'] });
    console.log(`PASS run-list audit ${width}px`);
  }
  assert.deepEqual(errors, [], 'Uncaught browser errors');
  writeFileSync(path.join(evidence, 'receipt.json'), JSON.stringify({
    head: execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(),
    fixture: 'run-list-audit.mjs mock API, no device or live backend',
    results: receipt
  }, null, 2));
} finally {
  clearTimeout(timeout);
  if (chrome.exitCode === null) {
    const stopped = new Promise((resolve) => chrome.once('exit', resolve));
    if (socket?.readyState === WebSocket.OPEN) await send('Browser.close').catch(() => chrome.kill());
    else chrome.kill();
    await stopped;
  }
  socket?.close();
  await new Promise((resolve) => server.close(resolve));
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
