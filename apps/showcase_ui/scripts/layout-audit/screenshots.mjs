// Before/after evidence: Workspace and Runs at 1280 and 1024 px, against the built UI + mock API.
//
//   npx ng build --configuration development && node scripts/layout-audit/screenshots.mjs <outDir> [label]
//
// Writes <outDir>/<label>-<page>-<width>.png. CHROME_BIN overrides the browser.
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startMockApi, mock, RUNS, PROMPT_CRITIQUE } from '../keyboard-walkthrough/mock-api.mjs';

const [outDir = 'screenshots', label = 'shot', audit] = process.argv.slice(2);
const checkControls = audit === '--controls';
const checkModels = audit === '--models';
const checkComposer = audit === '--composer';
const checkPrompts = audit === '--prompts';
const failures = [];
const iconNames = readFileSync(new URL('../../public/fonts/material-symbols-outlined.txt', import.meta.url), 'utf8').trim().split(',');
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
const pages = { workspace: '/workspace', runs: '/runs', 'run-detail': `/runs/${RUNS[1].session_id}`, setup: '/setup', check: '/check' };
if (checkPrompts) {
  for (const name of Object.keys(pages)) if (name !== 'run-detail') delete pages[name];
  pages.lessons = '/lessons';
  mock.critiques = [PROMPT_CRITIQUE];
}
if (checkControls) delete pages['run-detail'];
if (checkModels) for (const name of Object.keys(pages)) if (name !== 'workspace') delete pages[name];
if (checkComposer) for (const name of Object.keys(pages)) if (!['workspace', 'run-detail'].includes(name)) delete pages[name];
if (checkModels) for (const run of RUNS) run.agent_model = 'fixture-default';
if (checkModels) for (const session of mock.sessions) session.model_info = { name: 'Flash', id: 'fixture-default', provider: 'openai' };
for (const width of checkControls || checkModels || checkComposer || checkPrompts ? [1440, 390] : [1280, 1024]) {
  for (const [name, route] of Object.entries(pages)) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: checkPrompts ? 1400 : 800, deviceScaleFactor: 1, mobile: false });
    const previousImproveCalls = mock.improveCalls;
    await send('Page.navigate', { url: base + route });
    await sleep(2500);
    if (name === 'check') {
      await evaluate(`(() => {
        const element = document.querySelector('app-legacy-agent-stream');
        const component = ng.getComponent(element);
        component.agentService.sessionLogs.set([
          { type: 'step_recorded', timestamp: Date.now() / 1000, data: {
            summary: 'Verify the settings screen', operator_native_thinking: 'Find the settings control.',
            operator_raw_thinking: 'Open settings, then verify the saved state.',
            action_taken: { action: 'tap', target: 'Settings' }, last_execution_result: { success: true }
          } },
          { type: 'trace_recorded', timestamp: Date.now() / 1000, data: { message: 'Stream connected' } }
        ]);
        ng.applyChanges(element);
      })()`);
      const disclosureErrors = await evaluate(`(() => {
        const errors = [];
        for (const summary of document.querySelectorAll('app-legacy-agent-stream summary')) {
          const box = summary.getBoundingClientRect();
          const parent = summary.parentElement.getBoundingClientRect();
          if (box.width < parent.width - 6) errors.push(summary.textContent + ': disclosure does not span the card');
          if (box.height > 80) errors.push(summary.textContent + ': disclosure squeezed into a tall column');
        }
        return errors;
      })()`);
      failures.push(...disclosureErrors.map(error => `${name}-${width}: ${error}`));
    }
    await evaluate('document.fonts.ready.then(() => true)');
    if (checkPrompts) {
      if (mock.improveCalls !== previousImproveCalls) failures.push('Generation ran on page load');
      const errors = await evaluate(`(() => {
        const errors = [];
        const root = document.querySelector(${JSON.stringify(name === 'run-detail' ? 'app-prompt-improvement' : 'app-lessons')});
        if (!root) return ['Missing prompt feature'];
        if (${JSON.stringify(name === 'run-detail')} && !root.querySelector('textarea')?.value.includes('Continue as a guest')) errors.push('Saved critique not restored');
        for (const element of root.querySelectorAll('button, input, textarea, select, a')) {
          const box = element.getBoundingClientRect();
          if (box.width < 44 || box.height < 44) errors.push(element.tagName + ': target smaller than 44 px');
          if (box.left < 0 || box.right > innerWidth) errors.push(element.tagName + ': horizontal overflow');
        }
        if (document.documentElement.scrollWidth > innerWidth) errors.push('Page overflow');
        return errors;
      })()`);
      failures.push(...errors.map(error => `${name}-${width}: ${error}`));
      if (name === 'run-detail') await evaluate(`document.querySelector('.run-result-summary').scrollIntoView({ block: 'start' })`);
    }

    if (checkComposer && name === 'workspace') {
      await evaluate(`(() => {
        const textarea = document.querySelector('textarea.composer-input');
        textarea.value = 'Open Settings and check the accessibility toggles.\\n'.repeat(9).trim();
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      })()`);
      await sleep(100);
      if (label !== 'before') {
        const composerErrors = await evaluate(`(async () => {
          const textarea = document.querySelector('textarea.composer-input');
          const prompt = textarea.value;
          const errors = [];
          if (textarea.getBoundingClientRect().height <= 120) errors.push('Prompt did not grow past 120 px');
          textarea.value = prompt.repeat(20);
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          await new Promise(resolve => setTimeout(resolve, 100));
          if (Math.abs(textarea.getBoundingClientRect().height - innerHeight * 0.4) > 1) errors.push('Prompt must cap at 40% of viewport height');
          if (textarea.scrollHeight <= textarea.clientHeight || getComputedStyle(textarea).overflowY !== 'auto') errors.push('Long prompt must scroll internally');
          const composer = document.querySelector('.composer');
          if (composer.getBoundingClientRect().bottom > innerHeight + 1 || getComputedStyle(composer).position !== 'relative') errors.push('Composer must stay pinned in normal flow');
          textarea.value = '';
          textarea.dispatchEvent(new Event('input', { bubbles: true }));
          await new Promise(resolve => setTimeout(resolve, 100));
          if (Math.abs(textarea.getBoundingClientRect().height - 44) > 1) errors.push('Empty prompt must shrink to 44 px');
          ng.getComponent(document.querySelector('app-workspace')).startNewRunFrom(prompt);
          await new Promise(resolve => setTimeout(resolve, 100));
          if (textarea.getBoundingClientRect().height <= 120) errors.push('Restored prompt did not grow past 120 px');
          return errors;
        })()`);
        failures.push(...composerErrors.map(error => `${name}-${width}: ${error}`));
      }
    }
    if (checkComposer && label !== 'before') {
      const targetErrors = await evaluate(`(() => [...document.querySelectorAll('app-nav-switcher a, app-nav-switcher button, .run-row, .composer button, .composer select, .composer textarea, .status-strip button, .header-controls button, .header-controls summary')]
        .filter(element => element.checkVisibility())
        .filter(element => { const box = element.getBoundingClientRect(); return box.width < 44 || box.height < 44; })
        .map(element => element.className + ': target smaller than 44 px'))()`);
      failures.push(...targetErrors.map(error => `${name}-${width}: ${error}`));
    }
    if (checkModels) {
      const modelErrors = await evaluate(`(() => {
        const select = document.querySelector('select[aria-label="Run model"]');
        if (!select) return ['Model select is missing'];
        const errors = [];
        const box = select.getBoundingClientRect();
        if (box.width < 44 || box.height < 44) errors.push('Model select is smaller than 44 px');
        if (box.left < 0 || box.right > innerWidth) errors.push('Model select is outside the viewport');
        const send = document.querySelector('.btn-send').getBoundingClientRect();
        if (Math.abs(send.top - box.top) > 4) errors.push('Send and the model select must stay on one row');
        if (select.options.length !== 3 || !select.options[0].text.startsWith('Default')) errors.push('Default is not first or discovery is missing');
        select.value = select.options[2].value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        return errors;
      })()`);
      failures.push(...modelErrors.map(error => name + '-' + width + ': ' + error));
      await sleep(150);
    }
    const geometry = await evaluate(`(() => {
      const nav = document.querySelector('.floating-nav-switcher').getBoundingClientRect();
      const underNav = [];
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let node; (node = walker.nextNode());) {
        if (!node.textContent.trim() || node.parentElement.closest('.floating-nav-switcher, script, style, .material-symbols-outlined')) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const box of range.getClientRects()) {
          if (!box.width || !box.height || box.left >= nav.right || box.right <= nav.left || box.top >= nav.bottom || box.bottom <= nav.top) continue;
          let top = box.top, bottom = box.bottom, left = box.left, right = box.right;
          for (let parent = node.parentElement; parent; parent = parent.parentElement) {
            const style = getComputedStyle(parent);
            const clip = parent.getBoundingClientRect();
            if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) { top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom); }
            if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) { left = Math.max(left, clip.left); right = Math.min(right, clip.right); }
          }
          underNav.push({ text: node.textContent.trim().slice(0, 80), clipped: bottom <= top || right <= left || bottom <= nav.top || top >= nav.bottom || left >= nav.right || right <= nav.left });
        }
      }
      const targets = [...document.querySelectorAll('app-legacy-agent-stream select, app-legacy-agent-stream button, app-legacy-agent-stream summary')].map(element => {
        const box = element.getBoundingClientRect();
        return { label: element.textContent.trim().slice(0, 80), width: box.width, height: box.height };
      });
      return { overflowX: document.documentElement.scrollWidth - innerWidth, underNav, targets };
    })()`);
    writeFileSync(path.join(outDir, `${label}-${name}-${width}.json`), JSON.stringify(geometry, null, 2));
    if ((checkModels || checkComposer) && geometry.overflowX > 0) failures.push(name + '-' + width + ': horizontal overflow');
    if (name === 'workspace') {
      const iconErrors = await evaluate(`(() => {
        const errors = [];
        for (const name of ${JSON.stringify(iconNames)}) {
          const icon = document.createElement('span');
          icon.className = 'material-symbols-outlined';
          icon.textContent = name;
          document.body.appendChild(icon);
          const width = icon.getBoundingClientRect().width;
          if (Math.abs(width - 20) > 1) errors.push(name + ': icon did not shape to a single glyph (' + width + 'px)');
          icon.remove();
        }
        return errors;
      })()`);
      failures.push(...iconErrors.map(error => `${name}-${width}: ${error}`));
    }
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
        for (const element of document.querySelectorAll('.floating-nav-switcher, app-setup .panel, app-home .guide-card, .model-profile-toggle, .os-selector-pills, .empty-section-placeholder')) {
          if (visible(element) && getComputedStyle(element).boxShadow !== 'none') errors.push(element.className + ': surface shadow');
          if (visible(element) && parseFloat(getComputedStyle(element).borderTopWidth) > 0) errors.push(element.className + ': surface outline');
        }
        if (innerWidth <= 560 && ${JSON.stringify(name !== 'setup')}) {
          const activeTab = document.querySelector('.nav-tab-btn.active[aria-current="page"]');
          if (!activeTab) {
            errors.push('navigation: missing current-page tab');
          } else {
            const activeStyle = getComputedStyle(activeTab);
            if (activeStyle.backgroundImage === 'none' || activeStyle.backgroundSize !== '24px 3px' || activeStyle.backgroundPosition !== '50% 100%') errors.push('navigation: missing non-colour active underline');
          }
          for (const tab of document.querySelectorAll('.nav-tab-btn:not(.active)')) {
            if (getComputedStyle(tab).backgroundImage !== 'none') errors.push('navigation: inactive tab has an active underline');
          }
        }
        return errors;
      })()`);
      failures.push(...errors.map(error => `${name}-${width}: ${error}`));
      await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
      const focusErrors = await evaluate(`(async () => {
        const errors = [];
        for (const element of document.querySelectorAll('button:not(:disabled), app-legacy-agent-stream summary, app-legacy-agent-stream select, .nav-tab-btn')) {
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
    if (checkPrompts && name === 'run-detail') {
      mock.improveDelay = 300;
      mock.improveError = true;
      await evaluate(`document.querySelector('[data-improve]').click()`);
      await sleep(80);
      if (!await evaluate(`document.querySelector('[data-improve]').disabled && document.querySelector('[data-improve]').textContent.includes('Improving')`)) failures.push('Missing pending state');
      await sleep(400);
      if (!await evaluate(`document.querySelector('app-prompt-improvement [role="alert"]')?.textContent.includes('model unavailable')`)) failures.push('Missing generation error');
      mock.improveError = false;
      await evaluate(`document.querySelector('[data-improve]').click()`);
      await sleep(400);
      await evaluate(`(() => {
        const textarea = document.querySelector('app-prompt-improvement textarea');
        textarea.value = 'Edited checkout prompt';
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        const select = document.querySelector('app-prompt-improvement select');
        select.value = select.options[2].value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
      })()`);
      await sleep(50);
      await evaluate(`document.querySelector('[data-run-improved]').click()`);
      await sleep(500);
      const handoff = await evaluate(`(() => {
        const workspace = ng.getComponent(document.querySelector('app-workspace'));
        return { path: location.pathname, draft: workspace.taskInput, consumed: !history.state.autoStart,
          model: workspace.models.selection()?.model, picker: !!document.querySelector('.connection-panel, .phone-picker, .panel') };
      })()`);
      if (handoff.path !== '/workspace' || handoff.draft !== 'Edited checkout prompt' || !handoff.consumed || handoff.model !== 'gemini-via-proxy') failures.push('Invalid new-run handoff: ' + JSON.stringify(handoff));
      if (mock.improveCalls !== previousImproveCalls + 2) failures.push('Unexpected generation count');
      console.log(`PASS ${width}px: click-only generation, pending, error, retry, editable prompt, selected model and consumed auto-start`);
    }
    if (checkPrompts && name === 'lessons') {
      await evaluate(`(() => {
        const input = document.querySelector('#lesson-search');
        input.value = 'no matching lesson'; input.dispatchEvent(new Event('input', { bubbles: true }));
        document.querySelector('app-lessons form').requestSubmit();
      })()`);
      await sleep(100);
      if (!await evaluate(`document.querySelector('app-lessons').textContent.includes('No matching lessons')`)) failures.push('Search did not show empty state');
      mock.lessonsError = true;
      await evaluate(`document.querySelector('app-lessons form').requestSubmit()`);
      await sleep(100);
      if (!await evaluate(`!!document.querySelector('app-lessons [role="alert"]')`)) failures.push('Missing lessons error');
      mock.lessonsError = false;
      await evaluate(`document.querySelector('[data-retry]').click()`);
      await sleep(100);
      if (await evaluate(`!!document.querySelector('app-lessons [role="alert"]')`)) failures.push('Lessons retry failed');
      mock.critiques = [PROMPT_CRITIQUE];
      console.log(`PASS ${width}px: lessons search, empty, error and retry`);
    }

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
  console.log('PASS: control borders, shadows, 44 px hit boxes, keyboard focus, 3:1 focus contrast and mobile active underlines at 1440 px and 390 px.');
} else if (checkModels) {
  console.log('PASS: discovered model choice, default-first list, 44 px target and composer layout at 1440 px and 390 px. Fonts ready before capture.');
} else if (checkPrompts) {
  console.log('PASS: saved prompt result and lessons, 44 px targets and no overflow at 1440 and 390 px. Fonts ready before capture.');
} else if (checkComposer && label !== 'before') {
  console.log('PASS: composer growth, viewport cap, internal scrolling, shrink, pinned flow and 44 px targets at 1440 px and 390 px. Fonts ready before capture.');
}
