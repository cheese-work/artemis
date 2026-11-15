import assert from 'node:assert/strict';
import test from 'node:test';
import { mock, startMockApi } from './mock-api.mjs';

test('mock API serves host inventory alongside compact verdict results', async t => {
  const { server, url } = await startMockApi(process.cwd());
  const previous = { hosts: mock.hosts, results: mock.results };
  t.after(() => {
    Object.assign(mock, previous);
    return new Promise(resolve => server.close(resolve));
  });
  mock.hosts = { enabled: true, hosts: [{ id: 'fixture-host', status: 'online' }], devices: [] };
  mock.results = [{ session_id: 'fixture-run', results: [{ catalog_key: 'pass.verified' }] }];

  const results = await fetch(`${url}/api/chats/results`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ session_ids: ['fixture-run'] })
  });
  assert.equal(results.status, 200);
  assert.deepEqual(await results.json(), { sessions: mock.results });

  const hosts = await fetch(`${url}/api/hosts`);
  assert.equal(hosts.status, 200);
  assert.deepEqual(await hosts.json(), mock.hosts);
});

test('synthetic step screenshots show a usable payment screen', async t => {
  const { server, url } = await startMockApi(process.cwd());
  t.after(() => new Promise(resolve => server.close(resolve)));

  const response = await fetch(`${url}/images/post2.svg`);
  const screenshot = await response.text();

  assert.equal(response.status, 200);
  assert.match(screenshot, /<text\b/);
  assert.match(screenshot, /Payment/);
  assert.match(screenshot, /Order summary/);
});
