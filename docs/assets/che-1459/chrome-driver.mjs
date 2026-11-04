#!/usr/bin/node
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const flags = process.argv.slice(2);
const target = flags.find(flag => /^https?:/.test(flag));
const profile = flags.find(flag => flag.startsWith('--user-data-dir=')).split('=').slice(1).join('=');
const child = spawn('/usr/bin/google-chrome', ['--headless=new', '--no-sandbox', '--password-store=basic', '--disable-gpu', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'inherit', detached: true });
const stop = () => { try { process.kill(-child.pid, 'SIGTERM'); } catch {} };
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
child.on('exit', code => process.exit(code ?? 0));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  let port;
  for (let attempt = 0; attempt < 100 && child.exitCode === null; attempt++) {
    try { port = Number(readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]); break; } catch { await pause(100); }
  }
  const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(item => item.type === 'page');
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  await pause(500);
  socket.send(JSON.stringify({ id: 1, method: 'Page.navigate', params: { url: target } }));
  await pause(500);
  socket.close();
} catch (error) { console.error(error); stop(); }
