import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pasteShortcut } from './paste-shortcut.mjs';

for (const [platform, modifiers] of [['linux', 2], ['win32', 2], ['darwin', 4]]) {
  test(`${platform} paste sends a native clipboard editing command with the platform shortcut`, async () => {
    const events = [];
    await pasteShortcut(async (method, params) => events.push({ method, params }), platform);
    const shortcut = { key: 'v', code: 'KeyV', windowsVirtualKeyCode: 86, modifiers };
    assert.deepEqual(events, [
      { method: 'Input.dispatchKeyEvent', params: { type: 'rawKeyDown', ...shortcut, commands: ['paste'] } },
      { method: 'Input.dispatchKeyEvent', params: { type: 'keyUp', ...shortcut } }
    ]);
  });
}

test('a rejected native paste command fails the walkthrough instead of claiming a paste', async () => {
  const failure = new Error('Chrome rejected the paste command');
  await assert.rejects(pasteShortcut(async () => { throw failure; }, 'darwin'), failure);
});
