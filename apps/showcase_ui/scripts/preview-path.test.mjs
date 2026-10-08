import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi } from './keyboard-walkthrough/mock-api.mjs';

const dist = fileURLToPath(new URL('../dist/frontend/browser/', import.meta.url));
assert.ok(existsSync(path.join(dist, 'index.html')), 'Build the development UI first');
const html = readFileSync(path.join(dist, 'index.html'), 'utf8');
const fixtureSha = '1234567890abcdef1234567890abcdef12345678';
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const waitFor = async operation => {
  for (let attempt = 0; attempt < 100; attempt++) {
    const result = await operation();
    if (result) return result;
    await pause(100);
  }
  throw new Error('Fixture readiness timed out');
};

for (const prefix of ['/', '/preview/pr/70/']) {
  const { server, url } = await startMockApi(dist);
  const requests = [];
  const handler = server.listeners('request')[0];
  server.removeAllListeners('request');
  server.on('request', (request, response) => {
    requests.push(request.url);
    if (!request.url.startsWith(prefix)) {
      response.writeHead(404);
      return response.end();
    }
    request.url = request.url.slice(prefix.length - 1);
    const pathname = new URL(request.url, url).pathname;
    if (!pathname.startsWith('/api/') && !path.extname(pathname)) {
      response.writeHead(200, { 'content-type': 'text/html' });
      return response.end(html.replace(/<base href="[^"]*">/, `<base href="${prefix}">`)
        .replace('</head>', `<meta name="artemis-preview-sha" content="${fixtureSha}"></head>`));
    }
    handler(request, response);
  });
  server.on('upgrade', (request, socket) => {
    requests.push(request.url);
    socket.destroy();
  });
  const profile = mkdtempSync(path.join(tmpdir(), 'artemis-preview-path-'));
  const chrome = spawn(process.env.CHROME_BIN || 'google-chrome', [
    '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking', '--no-proxy-server',
    '--disable-extensions', '--disable-component-extensions-with-background-pages',
    '--host-resolver-rules=MAP *.googleapis.com ~NOTFOUND, MAP *.gstatic.com ~NOTFOUND',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'
  ], { stdio: ['ignore', 'ignore', 'pipe'] });
  let browserErrors = '';
  chrome.stderr.on('data', chunk => { browserErrors = (browserErrors + chunk).slice(-4000); });
  let socket;
  try {
    await waitFor(() => existsSync(path.join(profile, 'DevToolsActivePort')) || (chrome.exitCode !== null && assert.fail(browserErrors)));
    const port = readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = targets.find(target => target.type === 'page' && target.url === 'about:blank');
    assert.ok(page, 'A disposable browser page exists');
    socket = new WebSocket(page.webSocketDebuggerUrl);
    const opened = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Browser debugger connection timed out')), 10000);
      socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Browser debugger connection failed')); }, { once: true });
    });
    await opened;
    let nextId = 0;
    const pending = new Map();
    const runtimeErrors = [];
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') runtimeErrors.push(message.params);
      const promise = pending.get(message.id);
      if (!promise) return;
      pending.delete(message.id);
      if (message.error) promise.reject(new Error(JSON.stringify(message.error)));
      else promise.resolve(message.result);
    });
    const send = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++nextId;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`Browser command timed out: ${method}`)); }, 10000);
      pending.set(id, { resolve: result => { clearTimeout(timer); resolve(result); }, reject: error => { clearTimeout(timer); reject(error); } });
      socket.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async expression => {
      const response = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
      return response.result.value;
    };
    await send('Page.enable');
    await send('Runtime.enable');
    const navigation = await send('Page.navigate', { url: `${url}${prefix}runs/00000001-5d7e-4a10-9c33-0e1f2a3b4c5d` });
    if (navigation.errorText) {
      console.error(JSON.stringify({ navigation, requests, runtimeErrors, targets, location: await evaluate('location.href'), browserErrors }, null, 2));
      assert.fail(navigation.errorText);
    }
    try {
      await waitFor(() => evaluate(`!!window.ng && document.querySelector('app-run-viewer')?.innerText.includes('Log in and open settings')`).catch(() => false));
    } catch (error) {
      console.error(JSON.stringify({ requests, runtimeErrors, page: await evaluate('document.body.innerText') }, null, 2));
      throw error;
    }
    const banner = await evaluate(`document.querySelector('.preview-banner')?.textContent ?? null`);
    if (prefix === '/') assert.equal(banner, null);
    else assert.match(banner, /Synthetic preview.*PR 70.*1234567/);
    const bridge = await evaluate(`(() => {
      const root = ng.getComponent(document.querySelector('app-root'));
      root.agentService.ownerScope.setAllUsers(true);
      const relay = ng.getComponent(document.querySelector('app-nav-switcher')).usbRelay;
      const target = relay.createBridgeUrl();
      new WebSocket(target);
      return target;
    })()`);
    assert.equal(bridge, url.replace('http:', 'ws:') + prefix + 'api/device-bridge/session');
    await evaluate(`(async () => {
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { window.copiedRunLink = text; } } });
      const viewer = ng.getComponent(document.querySelector('app-run-viewer'));
      await viewer.copyLink();
    })()`);
    assert.equal(await evaluate('window.copiedRunLink'), url + prefix + 'runs/00000001-5d7e-4a10-9c33-0e1f2a3b4c5d');
    await waitFor(() => requests.some(request => request === `${prefix}api/stream?scope=all`));
    const images = await evaluate(`Array.from(document.querySelectorAll('app-run-viewer img')).map(image => image.getAttribute('src'))`);
    assert.ok(images.length > 0, 'Run screenshots were rendered');
    assert.ok(images.every(image => image.startsWith(prefix + 'images/')), JSON.stringify(images));
    if (prefix !== '/' && process.argv.includes('--root-api-negative-control')) await evaluate(`fetch('/api/preview-root-leak-control').catch(() => {})`);
    if (prefix !== '/') assert.equal(requests.filter(request => request.startsWith('/api/') || request.startsWith('/images/') || request.startsWith('/videos/') || request.startsWith('/local_file')).length, 0);
    assert.ok(requests.some(request => request.startsWith(`${prefix}api/runs/`)));
    console.log(`PASS ${prefix}: UI, banner, HTTP, scoped SSE, WebSocket, copied run link, images; root API escapes=0`);
  } finally {
    socket?.close();
    if (chrome.exitCode === null) {
      const exited = once(chrome, 'exit');
      chrome.kill('SIGTERM');
      await exited;
    }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}
