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
const checkProviders = audit === '--providers';
const checkChats = audit === '--chats';
const failures = [];
const iconNames = readFileSync(new URL('../../public/fonts/material-symbols-outlined.txt', import.meta.url), 'utf8').trim().split(',');
mkdirSync(outDir, { recursive: true });
const dist = process.env.ARTEMIS_SHOWCASE_DIST
  ? path.resolve(process.env.ARTEMIS_SHOWCASE_DIST)
  : fileURLToPath(new URL('../../dist/frontend/browser', import.meta.url));
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
const T0 = Date.parse('2026-10-20T12:00:00Z') / 1000;
if (checkProviders) {
  const root = 'https://congvc-c00.taila6fa8a.ts.net:31344';
  const official = { openai: 'https://api.openai.com/v1', google: 'https://generativelanguage.googleapis.com', anthropic: 'https://api.anthropic.com', xai: 'https://api.x.ai/v1', openrouter: 'https://openrouter.ai/api/v1' };
  mock.providers = {
    version: 'synthetic-provider-fixture',
    slots: Object.entries({ openai: 'OpenAI', google: 'Google', anthropic: 'Anthropic', xai: 'xAI', openrouter: 'OpenRouter' }).map(([provider, label], index) => ({
      provider, label, base_url: index ? official[provider] : root + '/v1', configured: index < 3, key_preview: index < 3 ? '****abcd' : null,
      enabled: index === 0 || index === 2, saved: true, model_count: index === 0 ? 12 : index === 2 ? 4 : 0,
      last_sync: index === 0 || index === 2 ? T0 : null, sync_error: null,
      last_test: index === 0 || index === 2 ? { ok: true, latency_ms: 248, error: null, at: T0 } : index === 1 ? { ok: false, latency_ms: 113, error: 'Google protocol request failed (HTTP 503). Gemini-native endpoint unavailable.', at: T0 } : null,
      presets: [{ name: 'Official', url: official[provider] }, { name: 'sub2api (C00)', url: root + (provider === 'google' || provider === 'anthropic' ? '' : '/v1') }]
    }))
  };
}
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
if (checkChats) {
  for (const name of Object.keys(pages)) if (!['runs', 'run-detail'].includes(name)) delete pages[name];
  const root = RUNS[1].session_id;
  const next = 'follow-up';
  RUNS[1].prompt = 'Check login, then continue on the same phone';
  const passResult = {
    catalog_key: 'pass.verified', verdict: 'pass', verification: 'checked', category: null,
    display_labels: { verification: 'Đã kiểm' }, reason: 'Guest login passed.',
    action: { text: '' }, responsible: '',
    evidence: { turn_id: root, step: null, screenshot_url: null },
    turn_index: 1, turn_count: 2, label_state: 'final', source: 'outcome'
  };
  const failResult = {
    catalog_key: 'fail.app_defect', verdict: 'fail', verification: 'self_reported', category: 'app_defect',
    display_labels: { verification: 'Agent tự báo', category: 'Lỗi ứng dụng' }, reason: 'Payment button is missing.',
    action: { text: 'Review the screen and report the missing payment control.' }, responsible: 'App under test',
    evidence: { turn_id: next, step: 2, screenshot_url: '/images/post2.svg' },
    turn_index: 2, turn_count: 2, label_state: 'final', source: 'outcome'
  };
  const historyPass = { ...passResult, evidence: { ...passResult.evidence, turn_id: RUNS[0].session_id }, turn_count: 1 };
  const historyFail = { ...failResult, evidence: { ...failResult.evidence, turn_id: root }, turn_index: 1, turn_count: 1 };
  mock.results = RUNS.map(run => ({
    session_id: run.session_id,
    results: run.session_id === RUNS[0].session_id ? [historyPass] : run.session_id === root ? [historyFail] : []
  }));
  mock.chat = { session_id: root, lease_pid: 123, idle_until: null,
    turns: [
      { turn_id: root, session_id: root, number: 1, status: 'completed', created_at: T0 },
      { turn_id: next, session_id: root, number: 2, status: 'completed', created_at: T0 + 90 }
    ],
    messages: [
      { sequence: 1, turn_id: root, role: 'human', kind: 'message', content: 'Check that guest login works.', acknowledged_at: T0, created_at: T0 },
      { sequence: 4, turn_id: next, role: 'human', kind: 'message', content: 'Now verify the payment control.', acknowledged_at: T0 + 90, created_at: T0 + 90 }
    ],
    timeline: [
      { sequence: 1, turn_id: root, role: 'human', kind: 'message', content: 'Check that guest login works.', created_at: T0 },
      { sequence: 2, turn_id: root, role: 'agent', kind: 'step', content: 'Open guest checkout', created_at: T0 + 1, payload: { step_number: 1, post_image_name: 'post1.svg' } },
      { sequence: 3, turn_id: root, role: 'agent', kind: 'step', content: 'Confirm the delivery details', created_at: T0 + 2, payload: { step_number: 2, post_image_name: 'post2.svg' } },
      { sequence: 4, turn_id: next, role: 'human', kind: 'message', content: 'Now verify the payment control.', created_at: T0 + 90 },
      { sequence: 5, turn_id: next, role: 'agent', kind: 'step', content: 'Enter the payment details', created_at: T0 + 91, payload: { step_number: 1, post_image_name: 'post1.svg' } },
      { sequence: 6, turn_id: next, role: 'agent', kind: 'step', content: 'Find the payment control', created_at: T0 + 92, payload: { step_number: 2, post_image_name: 'post2.svg', last_execution_result: { expected: 'Payment button is visible.', actual: 'Payment button is missing.' } } },
      { sequence: 7, turn_id: next, role: 'agent', kind: 'step', content: 'Confirm the order summary', created_at: T0 + 93, payload: { step_number: 3, post_image_name: 'post3.svg' } }
    ],
    results: [passResult, failResult],
    verdict: { status: 'completed', outcome: { tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 } } }
  };
}
if (checkPrompts) {
  for (const name of Object.keys(pages)) if (name !== 'run-detail') delete pages[name];
  pages.lessons = '/lessons';
  mock.critiques = [PROMPT_CRITIQUE];
}
if (checkProviders) {
  for (const name of Object.keys(pages)) delete pages[name];
  pages.providers = '/providers';
}
if (checkControls) delete pages['run-detail'];
if (checkModels) for (const name of Object.keys(pages)) if (name !== 'workspace') delete pages[name];
if (checkComposer) for (const name of Object.keys(pages)) if (!['workspace', 'run-detail'].includes(name)) delete pages[name];
if (checkModels) for (const run of RUNS) run.agent_model = 'fixture-default';
if (checkModels) for (const session of mock.sessions) session.model_info = { name: 'Flash', id: 'fixture-default', provider: 'openai' };
for (const width of checkChats ? [1440, 800, 390] : checkControls || checkModels || checkComposer || checkPrompts || checkProviders ? [1440, 390] : [1280, 1024]) {
  for (const [name, route] of Object.entries(pages)) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: checkChats ? (width === 390 ? 844 : 900) : checkPrompts ? 1400 : checkProviders ? (width === 390 ? 3450 : 2200) : 800, deviceScaleFactor: 1, mobile: checkChats && width === 390 });
    const previousImproveCalls = mock.improveCalls;
    await send('Page.navigate', { url: base + route });
    await sleep(2500);
    if (name === 'check') {
      await evaluate(`(() => {
        const element = document.querySelector('app-legacy-agent-stream');
        const component = ng.getComponent(element);
        component.agentService.sessionLogs.set([
          { type: 'step_recorded', timestamp: T0 + 120, data: {
            summary: 'Verify the settings screen', operator_native_thinking: 'Find the settings control.',
            operator_raw_thinking: 'Open settings, then verify the saved state.',
            action_taken: { action: 'tap', target: 'Settings' }, last_execution_result: { success: true }
          } },
          { type: 'trace_recorded', timestamp: T0 + 121, data: { message: 'Stream connected' } }
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
    if (checkChats) {
      const chatErrors = await evaluate(`(async () => {
        const errors = [];
        if (${JSON.stringify(name === 'runs')}) {
          const card = document.querySelector('[data-testid="run-card-verdict"]');
          if (!card || !card.textContent.includes('Pass')) errors.push('Missing verified history verdict');
          if (card?.querySelector('.run-verdict-category')) errors.push('Null category shown in history');
        } else {
          const verdicts = document.querySelectorAll('[data-testid="run-verdict"]');
          const selectedTurn = document.querySelector('.run-turn-selector button[aria-pressed="true"]');
          const selectedEvidence = document.querySelector('[data-testid="selected-step-evidence"]');
          const timeline = document.querySelector('[data-testid="run-step-timeline"]');
          const image = document.querySelector('[data-testid="run-evidence-screenshot"] img');
          const stepLinks = [...document.querySelectorAll('[data-testid="evidence-index"] a')];
          const stepRows = [...document.querySelectorAll('.step-row details')];
          if (verdicts.length !== 1 || verdicts[0].closest('.flat-timeline')) errors.push('Verdict is not a single page-level card');
          if (verdicts[0] && timeline && !(verdicts[0].compareDocumentPosition(timeline) & Node.DOCUMENT_POSITION_FOLLOWING)) errors.push('Verdict card follows timeline scrollback');
          if (!selectedTurn || selectedTurn.getAttribute('data-turn-id') !== 'follow-up') errors.push('Latest historical turn is not selected');
          if (!selectedEvidence?.textContent.includes('Payment button is missing')) errors.push('Selected-turn evidence is missing');
          if (image) { image.loading = 'eager'; await image.decode().catch(() => {}); }
          if (!image || image.naturalWidth < 200) errors.push('Usable synthetic evidence screenshot is missing');
          if (!stepLinks.length || stepLinks.some(link => !link.getAttribute('href')?.startsWith('#step-') || !document.querySelector(link.getAttribute('href')))) errors.push('Step index links are not real anchors');
          if (stepLinks.length && !['Enter the payment details', 'Find the payment control', 'Confirm the order summary'].every(title => stepLinks.some(link => link.textContent.includes(title)))) errors.push('Step index omits actual step titles');
          if (stepRows.length < 3 || stepRows.filter(details => details.open).length !== 1) errors.push('Earlier steps are not individually collapsed');
          if (selectedEvidence && stepLinks[0] && !(selectedEvidence.compareDocumentPosition(stepLinks[0]) & 4)) errors.push('F5 evidence order is incorrect');
          if (${width} === 1440) {
            const evidenceGrid = document.querySelector('[data-testid="run-decisive-evidence"]');
            const stepIndex = document.querySelector('[data-testid="evidence-index"]');
            const evidenceImage = document.querySelector('[data-testid="run-evidence-screenshot"]');
            const gridWidth = evidenceGrid?.getBoundingClientRect().width ?? 0;
            if (!gridWidth || (stepIndex?.getBoundingClientRect().width ?? 0) < Math.max(200, gridWidth * 0.4)) errors.push('1440px step titles do not receive usable width');
            if (!gridWidth || (evidenceImage?.getBoundingClientRect().width ?? 0) < Math.max(200, gridWidth * 0.4)) errors.push('1440px evidence screenshot does not receive usable width');
          }
          if (!document.querySelector('[data-testid="run-chat-composer"]')) errors.push('Chat composer is missing');
          if (document.body.textContent.includes('Session details could not load.')) errors.push('Session sidebar fixture is unavailable');
          if (${width} === 390) {
            const history = document.querySelector('.chat-history');
            const sidebar = document.querySelector('app-session-sidebar');
            const sidebarTabs = [...(sidebar?.querySelectorAll('[role="tab"]') ?? [])].map(tab => tab.textContent.trim());
            const labels = [...(selectedEvidence?.querySelectorAll('strong') ?? [])].filter(label => ['Mong đợi', 'Thực tế'].includes(label.textContent.trim()));
            const withinViewport = element => {
              const box = element.getBoundingClientRect();
              const clip = history.getBoundingClientRect();
              return box.width > 0 && box.height > 0 && box.top >= Math.max(0, clip.top) && box.bottom <= Math.min(innerHeight, clip.bottom);
            };
            if (history?.scrollTop !== 0) errors.push('Initial 390px view auto-scrolled away from the selected-turn card');
            if (labels.length !== 2 || labels.some(label => !withinViewport(label))) errors.push('Initial 390x844 view does not show Mong đợi and Thực tế');
            if (!sidebar || sidebarTabs.join('|') !== 'Live|Details|Stats') errors.push('Compact evidence tabs are missing');
            if (sidebar && sidebar.getBoundingClientRect().height > 55 && sidebar.querySelector('.muted')) errors.push('Empty media panel consumes the compact evidence viewport');
          }
        }
        if (document.documentElement.scrollWidth > innerWidth) errors.push('Horizontal overflow');
        for (const button of document.querySelectorAll('app-run-chat button')) {
          const box = button.getBoundingClientRect();
          if (box.width < 44 || box.height < 44) errors.push('Small action target');
        }
        return errors;
      })()`);
      failures.push(...chatErrors.map(error => `${name}-${width}: ${error}`));
    }
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

    if (checkProviders) {
      const providerErrors = await evaluate(`(() => {
        const errors = [];
        if (document.querySelectorAll('app-providers article').length !== 5) errors.push('Missing provider cards');
        if ([...document.querySelectorAll('input[type=password]')].some(input => input.value)) errors.push('Key field is not blank');
        if (document.documentElement.scrollWidth > innerWidth) errors.push('Horizontal overflow');
        return errors;
      })()`);
      failures.push(...providerErrors.map(error => name + '-' + width + ': ' + error));
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
    const initialName = `${label}-${name}-initial-${width}.png`;
    writeFileSync(path.join(outDir, initialName), Buffer.from(data, 'base64'));
    console.log(initialName);
    if (checkChats && name === 'run-detail') {
      const initial = await evaluate(`(() => {
        const history = document.querySelector('.chat-history');
        const card = document.querySelector('[data-testid="run-verdict"]');
        const timeline = document.querySelector('[data-testid="run-step-timeline"]');
        const evidence = document.querySelector('[data-testid="selected-step-evidence"]');
        const evidenceGrid = document.querySelector('[data-testid="run-decisive-evidence"]');
        const stepIndex = document.querySelector('[data-testid="evidence-index"]');
        const evidenceImage = document.querySelector('[data-testid="run-evidence-screenshot"]');
        const sidebar = document.querySelector('app-session-sidebar');
        const composer = document.querySelector('[data-testid="run-chat-composer"]');
        const labels = [...(evidence?.querySelectorAll('strong') ?? [])].filter(label => ['Mong đợi', 'Thực tế'].includes(label.textContent.trim()));
        const rect = element => { const box = element.getBoundingClientRect(); return { top: box.top, bottom: box.bottom, left: box.left, right: box.right, width: box.width, height: box.height }; };
        return { width: innerWidth, height: innerHeight, scrollTop: history?.scrollTop ?? null, card: card && rect(card), timeline: timeline && rect(timeline), evidenceGrid: evidenceGrid && rect(evidenceGrid), stepIndex: stepIndex && rect(stepIndex), evidenceImage: evidenceImage && rect(evidenceImage), sidebar: sidebar && rect(sidebar), composer: composer && rect(composer), labels: labels.map(label => ({ text: label.textContent.trim(), bounds: rect(label) })) };
      })()`);
      writeFileSync(path.join(outDir, `${label}-run-detail-initial-${width}.json`), JSON.stringify(initial, null, 2) + '\n');
      const focused = await evaluate(`(async () => {
        const errors = [];
        const link = document.querySelector('[data-testid="evidence-index"] a[aria-current="step"]');
        const target = link && document.getElementById(decodeURIComponent(link.hash.slice(1)));
        if (!link || !target) return { errors: ['Selected evidence anchor is missing'], width: innerWidth, height: innerHeight };
        link.click();
        await new Promise(resolve => setTimeout(resolve, 200));
        const evidence = target.querySelector('.evidence');
        const before = target.querySelector('.evidence-before');
        const after = target.querySelector('.evidence-after');
        const image = after?.querySelector('img.step-screenshot');
        if (image) { image.loading = 'eager'; await image.decode().catch(() => {}); }
        if (!target.querySelector('details')?.open) errors.push('Focused evidence step did not open');
        if (before) errors.push('Empty Before panel consumes the evidence viewport');
        if (!image || image.naturalWidth < 200) errors.push('Focused evidence screenshot is unavailable');
        if (evidence && after && after.getBoundingClientRect().width < evidence.getBoundingClientRect().width * .75) errors.push('Selected After screenshot does not use the available evidence width');
        const rect = element => { const box = element.getBoundingClientRect(); return { top: box.top, bottom: box.bottom, left: box.left, right: box.right, width: box.width, height: box.height }; };
        return { errors, width: innerWidth, height: innerHeight, scrollTop: document.querySelector('.chat-history')?.scrollTop ?? null, evidence: evidence && rect(evidence), after: after && rect(after), image: image && { naturalWidth: image.naturalWidth, naturalHeight: image.naturalHeight }, sidebar: document.querySelector('app-session-sidebar') && rect(document.querySelector('app-session-sidebar')) };
      })()`);
      failures.push(...focused.errors.map(error => `run-detail-${width}: ${error}`));
      writeFileSync(path.join(outDir, `${label}-run-detail-selected-evidence-focused-${width}.json`), JSON.stringify(focused, null, 2) + '\n');
      const { data: evidenceData } = await send('Page.captureScreenshot', { format: 'png' });
      writeFileSync(path.join(outDir, `${label}-run-detail-selected-evidence-focused-${width}.png`), Buffer.from(evidenceData, 'base64'));
    }
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
