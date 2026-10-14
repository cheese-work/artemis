// A tiny stand-in for the SmartQA server: serves the built UI and just enough API
// for the run library and viewer. No video (the viewer shows its screenshot fallback).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const T0 = Math.floor(Date.now() / 1000) - 3600;
const id = (n) => `${String(n).padStart(8, '0')}-5d7e-4a10-9c33-0e1f2a3b4c5d`;
const PROMPTS = [
  ['Log in and open settings', 'completed'],
  ['Add an item to the cart and check out as a guest', 'failed'],
  ['Turn on dark mode', 'interrupted'],
  ['Search for headphones and sort by price', 'completed'],
  ['Cancel an order from history', 'cancelled'],
  ['Open the camera permission dialog', 'completed']
];
export const RUNS = PROMPTS.map(([prompt, status], i) => ({
  session_id: id(i + 1),
  prompt,
  status,
  interrupt_reason: status === 'interrupted' ? 'host_disconnected' : null,
  start_time: T0 - i * 7200,
  end_time: T0 - i * 7200 + 300,
  host_id: null,
  device_ref: { host_id: null, serial: 'emulator-5554' },
  requested_by: 'qa@example.test',
  pinned: false,
  recordings: []
}));
// Someone else's run: only the admin's "All users" view lists it.
const OTHERS = [{ ...RUNS[0], session_id: id(7), prompt: "Another QA's checkout test", requested_by: 'other@example.test' }];
const STEPS = Array.from({ length: 4 }, (_, n) => ({
  step_id: `st${n + 1}`,
  step_number: n + 1,
  timestamp: T0 + n,
  action_taken: { action: 'tap' },
  pre_image_name: `pre${n + 1}.svg`,
  post_image_name: `post${n + 1}.svg`
}));

// Mutable so layout audits can drive connected / disconnected / error states. Defaults match the keyboard walkthrough.
export const PROMPT_CRITIQUE = {
  id: 'critique-checkout', session_id: RUNS[1].session_id, created_at: T0 + 400,
  requested_by: 'qa@example.test', model: 'openai/fixture-default',
  original_prompt: RUNS[1].prompt,
  improved_prompt: 'Add one available item to the cart. Continue as a guest, enter the provided delivery details, and stop before placing the order. Verify that the order summary shows the item, delivery cost, and total.',
  changes: [
    { what: 'Define where checkout ends', why: 'The original prompt did not say whether to place a real order.' },
    { what: 'Name the evidence to verify', why: 'The run stopped at the delivery screen without checking the order summary.' }
  ],
  lessons: ['Give the task a clear stopping point.', 'Name the visible result that proves the check passed.']
};
export const mock = { chat: null, streamState: null, streamImage: null, critiques: [], lessonsError: false, improveCalls: 0, improveError: false, improveDelay: 0, sessions: [], status: null, failSessions: false, failVideo: false, identity: null, readiness: null, config: null, providers: null };

const json = (res, code, body) => {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
};

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

export function startMockApi(distDir) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const p = url.pathname;
    if (p === '/api/stream/device-state') return json(res, 200, mock.streamState ?? { connected: false, serial: null, live_stream_url: null });
    if (p === '/api/stream/device-live' && mock.streamImage) {
      res.writeHead(200, { 'content-type': 'image/png' });
      return res.end(Buffer.from(mock.streamImage, 'base64'));
    }
    if (p === '/api/prompt-lessons') {
      if (mock.lessonsError) return json(res, 500, { detail: 'Could not load lessons' });
      const q = (url.searchParams.get('q') ?? '').toLowerCase();
      const rows = mock.critiques.filter(row => JSON.stringify(row).toLowerCase().includes(q));
      return json(res, 200, { critiques: [...rows].sort((a, b) => b.created_at - a.created_at), next_offset: null });
    }
    const evidence = p.match(/^\/api\/sessions\/([^/]+)\/(checks|notes|prompt-critique|improve-prompt)$/);
    if (evidence) {
      const [, sessionId, kind] = evidence;
      if (kind === 'checks') return json(res, 200, { records: [], streams: [], run_outcome: { task_status: 'failed', tests: { passed: 0, failed: 1, inconclusive: 0, unchecked: 0 }, last_findings: ['The order summary was not reached.'] } });
      if (kind === 'notes') return json(res, 200, {});
      if (kind === 'prompt-critique') {
        const critique = mock.critiques.find(row => row.session_id === sessionId);
        return json(res, critique ? 200 : 404, critique ?? { code: 'prompt_critique_not_found' });
      }
      if (kind === 'improve-prompt' && req.method === 'POST') {
        mock.improveCalls++;
        return setTimeout(() => {
          if (mock.improveError) return json(res, 502, { code: 'improve_model_failed', detail: 'openai/fixture-default: model unavailable. Try again.' });
          const critique = { ...PROMPT_CRITIQUE, session_id: sessionId, id: `critique-${mock.improveCalls}`, created_at: Date.now() / 1000 };
          mock.critiques.unshift(critique);
          json(res, 200, critique);
        }, mock.improveDelay);
      }
    }
    if (p === '/api/system/providers' && mock.providers) return json(res, 200, mock.providers);
    if (p === '/api/models') return json(res, 200, {
      default: { provider: 'openai', model: 'fixture-default' },
      models: [
        { provider: 'openai', model: 'fixture-default', source: 'fixture' },
        { provider: 'openai', model: 'gemini-via-proxy', source: 'fixture' }
      ], errors: []
    });
    if (p === '/api/runs') {
      const q = (url.searchParams.get('q') ?? '').toLowerCase();
      const status = url.searchParams.get('status');
      const all = ['all', 'everyone'].includes(url.searchParams.get('scope'));
      const runs = (all ? [...RUNS, ...OTHERS] : RUNS).filter((r) => (!q || r.prompt.toLowerCase().includes(q)) && (!status || r.status === status));
      return json(res, 200, { runs, next_cursor: null, warnings: [] });
    }
    let m = p.match(/^\/api\/chats\/([^/]+)$/);
    if (m) {
      if (mock.chat?.turns.some(turn => turn.turn_id === m[1])) return json(res, 200, mock.chat);
      const run = [...RUNS, ...OTHERS].find(row => row.session_id === m[1]);
      const messages = run ? [{ sequence: 1, turn_id: run.session_id, session_id: run.session_id, role: 'human', kind: 'message', content: run.prompt, client_id: 'opening', reply_to: null, acknowledged_at: run.start_time, created_at: run.start_time }] : [];
      return run ? json(res, 200, { session_id: run.session_id, lease_pid: null, idle_until: null,
        turns: [{ turn_id: run.session_id, session_id: run.session_id, number: 1, status: run.status, created_at: run.start_time }],
        messages,
        timeline: [...messages, ...STEPS.map((step, index) => ({ sequence: index + 2, turn_id: run.session_id, role: 'agent', kind: 'step', content: `Step ${index + 1}`, created_at: step.timestamp, payload: { ...step, session_id: run.session_id } }))],
        verdict: { status: run.status, outcome: { phase: 'outcome', tests: { passed: run.status === 'completed' ? 1 : 0, failed: run.status === 'failed' ? 1 : 0, inconclusive: 0, unchecked: 0 }, last_findings: ['Synthetic fixture result'] }, report: 'Synthetic agent report', details: run }
      }) : json(res, 404, { detail: 'Chat not found.' });
    }
    m = p.match(/^\/api\/runs\/([^/]+)$/);
    if (m) {
      const run = [...RUNS, ...OTHERS].find((r) => r.session_id.startsWith(m[1]));
      return run ? json(res, 200, run) : json(res, 404, { error: 'not_found' });
    }
    m = p.match(/^\/api\/sessions\/([^/]+)\/steps$/);
    if (m) return json(res, 200, STEPS.map((s) => ({ ...s, session_id: m[1] })));
    m = p.match(/^\/api\/sessions\/([^/]+)\/video$/);
    if (m && mock.failVideo) return json(res, 500, { error: 'boom' });
    if (m) return json(res, 200, { session_id: m[1], status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
    if (p === '/api/system/whoami') return mock.identity ? json(res, mock.identity.code ?? 200, mock.identity.body) : json(res, 200, { email: 'qa@example.test', admin: true, auth_mode: 'cloudflare', reason: null });
    if (p === '/api/system/readiness' && mock.readiness) return json(res, 200, mock.readiness);
    if (p === '/api/system/config' && mock.config) return json(res, 200, mock.config);
    if (p === '/api/status' && mock.status) return json(res, 200, mock.status);
    if (p === '/api/sessions' && mock.failSessions) return json(res, 500, { error: 'boom' });
    if (p === '/api/sessions' && mock.sessions.length) return json(res, 200, mock.sessions);

    if (p === '/api/hosts') return json(res, 200, { enabled: true, hosts: [], devices: [] });
    if (p.startsWith('/images/')) {
      res.writeHead(200, { 'content-type': 'image/svg+xml' });
      return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="90" height="160"><rect width="90" height="160" fill="#dbeafe"/></svg>');
    }
    if (p.startsWith('/api/')) return json(res, p === '/api/sessions' ? 200 : 404, p === '/api/sessions' ? [] : {});
    const file = path.join(distDir, p === '/' || !path.extname(p) ? 'index.html' : p);
    fs.readFile(file, (err, data) => {
      if (err) {
        res.writeHead(404);
        return res.end();
      }
      res.writeHead(200, { 'content-type': TYPES[path.extname(file)] ?? 'application/octet-stream' });
      res.end(data);
    });
  });
  return new Promise((resolve) =>
    server.listen(0, '127.0.0.1', () => resolve({ server, url: `http://127.0.0.1:${server.address().port}` }))
  );
}
