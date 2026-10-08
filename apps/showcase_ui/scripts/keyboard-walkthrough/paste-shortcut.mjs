export async function pasteShortcut(send, platform = process.platform) {
  const shortcut = {
    key: 'v', code: 'KeyV', windowsVirtualKeyCode: 86,
    modifiers: platform === 'darwin' ? 4 : 2
  };
  await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...shortcut, commands: ['paste'] });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...shortcut });
}
