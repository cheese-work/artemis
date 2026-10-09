import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi } from './keyboard-walkthrough/mock-api.mjs';

const dist = process.env.SHELL_DIST || fileURLToPath(new URL('../dist/frontend/browser', import.meta.url));
assert.ok(existsSync(path.join(dist, 'index.html')), 'Build the development UI first');
const { server, url } = await startMockApi(dist);
const profile = mkdtempSync(path.join(tmpdir(), 'responsive-shell-'));
const port = 9222 + Math.floor(Math.random() * 2000);
const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--password-store=basic', '--no-proxy-server',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'
], { stdio: 'ignore' });
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
let socket;
let nextId = 1;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const { result, exceptionDetails } = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  assert.ok(!exceptionDetails, exceptionDetails?.exception?.description);
  return result.value;
};
const click = async selector => {
  const point = await evaluate(`(() => {
    const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 });
  await delay(150);
};
const escape = async () => {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' });
};
const visibleBoxes = async selector => evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector)}))
  .filter(element => element.getClientRects().length)
  .map(element => {
    const rect = element.getBoundingClientRect();
    return { label: element.getAttribute('aria-label') || element.textContent.trim(),
      left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height };
  })`);
const assertTargets = boxes => {
  assert.ok(boxes.length > 0, 'Controls must exist');
  for (const box of boxes) {
    assert.ok(box.width >= 44 && box.height >= 44, `${box.label}: ${box.width} × ${box.height}`);
    for (const other of boxes) {
      if (box === other) continue;
      assert.ok(box.bottom <= other.top || other.bottom <= box.top || box.right <= other.left || other.right <= box.left,
        `${box.label} overlaps ${other.label}`);
    }
  }
};
const evidence = [];
if (process.env.SHOTS) mkdirSync(process.env.SHOTS, { recursive: true });

try {
  let tabs;
  for (let attempt = 0; attempt < 100; attempt++) {
    tabs = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json()).catch(() => null);
    if (tabs?.length) break;
    await delay(100);
  }
  assert.ok(tabs?.length, 'Headless Chrome must start');
  socket = new WebSocket(tabs.find(tab => tab.type === 'page').webSocketDebuggerUrl);
  await once(socket, 'open');
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Runtime.exceptionThrown') console.error(message.params.exceptionDetails.exception?.description);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    if (message.error) request.reject(new Error(message.error.message));
    else request.resolve(message.result);
  });
  await send('Page.enable');
  await send('Runtime.enable');
  await delay(300);
  const navigation = await send('Page.navigate', { url: `${url}/workspace` });
  assert.ok(!navigation.errorText, navigation.errorText);
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await evaluate(`!!window.ng && !!document.querySelector('app-admin-identity-indicator summary')`).catch(() => false)) break;
    await delay(100);
  }
  await evaluate('document.fonts.ready');
  assert.ok(await evaluate(`!!document.querySelector('.app-page')`), await evaluate(`JSON.stringify({ url: location.href, html: document.documentElement.outerHTML.slice(0, 1500) })`));

  for (const width of [1440, 1200, 1199, 1024, 1023, 800, 799, 390, 320]) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 900, deviceScaleFactor: 1, mobile: width < 800 });
    await delay(150);
    const layout = await evaluate(`(() => {
      const nav = document.querySelector('.floating-nav-switcher');
      const header = document.querySelector('.navigation-header');
      const page = document.querySelector('.app-page');
      const rect = nav.getBoundingClientRect();
      const pageRect = page.getBoundingClientRect();
      return { nav: { width: rect.width, height: rect.height, top: rect.top, bottom: rect.bottom },
        headerHeight: header?.getBoundingClientRect().height,
        page: { left: pageRect.left, top: pageRect.top, bottom: pageRect.bottom },
        clearance: parseFloat(getComputedStyle(page).getPropertyValue('--nav-clearance')),
        footer: !!document.querySelector('app-root > app-version-footer'),
        labels: Array.from(nav.querySelectorAll('.tab-label')).every(label => label.getClientRects().length > 0),
        labelSize: getComputedStyle(nav.querySelector('.tab-label')).fontSize,
        activeBackgroundImage: getComputedStyle(nav.querySelector('.nav-tab-btn.active')).backgroundImage,
        overflow: document.documentElement.scrollWidth - innerWidth };
    })()`);
    assert.equal(layout.footer, false, `${width}: no fixed version footer`);
    assert.equal(layout.overflow, 0, `${width}: no viewport overflow`);
    assert.equal(layout.labels, true, `${width}: labels stay visible`);
    assert.match(layout.activeBackgroundImage, /linear-gradient/, `${width}: active nav selection bar`);
    if (width >= 800) {
      assert.equal(layout.nav.width, width < 1200 ? 72 : 224, `${width}: sidebar/rail width`);
      assert.equal(layout.page.left, layout.nav.width, `${width}: content clears sidebar/rail`);
      assert.equal(layout.clearance, 0, `${width}: no stale floating-nav clearance`);
    } else {
      assert.equal(layout.headerHeight, 48, `${width}: top bar height`);
      assert.equal(layout.nav.height, 56, `${width}: tab bar height`);
      assert.equal(layout.nav.bottom, 900, `${width}: tab bar reaches bottom`);
      assert.equal(layout.page.top, 48, `${width}: content clears top bar`);
      assert.equal(layout.page.bottom, 844, `${width}: composer area clears tab bar`);
      assert.equal(layout.page.left, 0, `${width}: no rail margin`);
      assert.equal(layout.clearance, 0, `${width}: no duplicate page padding`);
    }
    if (width < 1200) assert.equal(layout.labelSize, '12px', `${width}: rail/tab labels`);
    const targets = await visibleBoxes('.nav-tab-btn, button.chip, .identity-indicator');
    assertTargets(targets);
    const labels = await evaluate(`Array.from(document.querySelectorAll('.nav-tab-btn')).map(element => {
      const box = element.getBoundingClientRect();
      const label = element.querySelector('.tab-label').getBoundingClientRect();
      return { text: element.querySelector('.tab-label').textContent, inside: label.left >= box.left && label.right <= box.right,
        box: { left: box.left, right: box.right }, label: { left: label.left, right: label.right } };
    })`);
    assert.ok(labels.every(label => label.inside), `${width}: labels fit inside their controls: ${JSON.stringify(labels)}`);
    if (width < 800) {
      assert.equal((await visibleBoxes('.chip-visual'))[0].height, 32, `${width}: 32 px device visual`);
      assert.deepEqual(await evaluate(`['borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth']
        .map(property => getComputedStyle(document.querySelector('.chip-visual'))[property])`),
        ['0px', '0px', '0px', '0px'], `${width}: borderless device visual`);
      assert.equal((await visibleBoxes('.identity-avatar'))[0].height, 28, `${width}: 28 px avatar visual`);
    }
    for (const target of targets) {
      assert.ok(target.left >= 0 && target.right <= width && target.top >= 0 && target.bottom <= 900,
        `${width}: ${target.label} stays on screen`);
    }
    await click('button.chip');
    const phonePanel = (await visibleBoxes('.panel'))[0];
    assert.ok(phonePanel.left >= 0 && phonePanel.right <= width && phonePanel.top >= 0 && phonePanel.bottom <= 900,
      `${width}: phone picker stays on screen`);
    assertTargets(await visibleBoxes('.panel button, .panel a'));
    await escape();
    await click('.identity-indicator');
    const accountPanel = (await visibleBoxes('.identity-panel'))[0];
    assert.ok(accountPanel.left >= 0 && accountPanel.right <= width && accountPanel.top >= 0 && accountPanel.bottom <= 900,
      `${width}: account menu stays on screen`);
    assertTargets(await visibleBoxes('.identity-panel a, .identity-panel button'));
    await escape();
    await evaluate(`(() => {
      const page = document.querySelector('.app-page');
      for (const pane of ['list', 'detail']) {
        const element = document.createElement('div');
        element.dataset.shellPane = pane;
        element.textContent = pane;
        page.append(element);
      }
      const root = document.querySelector('app-root');
      ng.getComponent(root).shell.activePane.set('detail');
      ng.applyChanges(root);
    })()`);
    assert.deepEqual(await evaluate(`Array.from(document.querySelectorAll('[data-shell-pane]'))
      .filter(element => getComputedStyle(element).display !== 'none').map(element => element.dataset.shellPane)`),
      width < 1024 ? ['detail'] : ['list', 'detail'], `${width}: single-pane hook`);
    await click('a[aria-label="Workspace"]');
    assert.equal(await evaluate(`document.querySelector('.app-page').dataset.activePane`), 'list', `${width}: nav resets pane`);
    await evaluate(`document.querySelectorAll('[data-shell-pane]').forEach(element => element.remove())`);
    await evaluate(`ng.getComponent(document.querySelector('app-root')).shell.activePane.set('detail')`);
    await click('a[aria-label="Runs"]');
    assert.equal(await evaluate(`location.pathname`), '/runs', `${width}: native Runs navigation`);
    assert.equal(await evaluate(`document.querySelector('.app-page').dataset.activePane`), 'list', `${width}: Runs resets pane`);
    await click('a[aria-label="Workspace"]');
    assert.equal(await evaluate(`location.pathname`), '/workspace', `${width}: native Workspace navigation`);
    if (process.env.SHOTS && [1440, 1199, 1024, 800, 799, 390].includes(width)) {
      const { data } = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(process.env.SHOTS, `shell-${width}.png`), Buffer.from(data, 'base64'));
    }
    evidence.push({ width, layout, targets });
    console.log(`PASS ${width}px: active nav linear-gradient, shell, pane switch, hit boxes, phone picker and account menu${width < 800 ? ', borderless device visual' : ''}`);
  }
  if (process.env.SHOTS) writeFileSync(path.join(process.env.SHOTS, 'assertions.json'), JSON.stringify(evidence, null, 2));
} finally {
  const stopped = once(chrome, 'exit');
  if (socket?.readyState === WebSocket.OPEN) await send('Browser.close');
  else chrome.kill();
  await stopped;
  socket?.close();
  await new Promise(resolve => server.close(resolve));
  rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
