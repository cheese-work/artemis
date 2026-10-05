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
const RUNS = PROMPTS.map(([prompt, status], i) => ({
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

const json = (res, code, body) => {
  res.writeHead(code, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
};

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

export function startMockApi(distDir) {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const p = url.pathname;
    if (p === '/api/runs') {
      const q = (url.searchParams.get('q') ?? '').toLowerCase();
      const status = url.searchParams.get('status');
      const all = url.searchParams.get('scope') === 'all';
      const runs = (all ? [...RUNS, ...OTHERS] : RUNS).filter((r) => (!q || r.prompt.toLowerCase().includes(q)) && (!status || r.status === status));
      return json(res, 200, { runs, next_cursor: null, warnings: [] });
    }
    let m = p.match(/^\/api\/runs\/([^/]+)$/);
    if (m) {
      const run = [...RUNS, ...OTHERS].find((r) => r.session_id.startsWith(m[1]));
      return run ? json(res, 200, run) : json(res, 404, { error: 'not_found' });
    }
    m = p.match(/^\/api\/sessions\/([^/]+)\/steps$/);
    if (m) return json(res, 200, STEPS.map((s) => ({ ...s, session_id: m[1] })));
    m = p.match(/^\/api\/sessions\/([^/]+)\/video$/);
    if (m) return json(res, 200, { session_id: m[1], status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
    if (p === '/api/system/whoami') return json(res, 200, { email: 'qa@example.test', admin: true, auth_mode: 'cloudflare', reason: null });
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
