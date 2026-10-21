import assert from 'node:assert/strict';
import test from 'node:test';
import { startMockApi } from './mock-api.mjs';

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
