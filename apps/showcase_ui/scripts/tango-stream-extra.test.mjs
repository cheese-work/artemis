import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';

test('real wrapped streams work after async downleveling', async () => {
  const result = await build({
    stdin: {
      contents: `
        import { WrapReadableStream } from '@yume-chan/stream-extra/esm/wrap-readable.js';
        import { WrapWritableStream } from '@yume-chan/stream-extra/esm/wrap-writable.js';
        export { WrapReadableStream, WrapWritableStream };
      `,
      resolveDir: process.cwd(),
      sourcefile: 'tango-stream-extra-regression.mjs'
    },
    bundle: true,
    format: 'esm',
    platform: 'node',
    target: 'esnext',
    supported: { 'async-await': false },
    write: false
  });
  const { WrapReadableStream, WrapWritableStream } = await import(
    `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`
  );

  const wrappedReadable = new WrapReadableStream(
    new ReadableStream({
      start(controller) {
        controller.enqueue('read');
        controller.close();
      }
    })
  );
  const reader = wrappedReadable.getReader();

  assert.deepEqual(await reader.read(), { done: false, value: 'read' });
  assert.deepEqual(await reader.read(), { done: true, value: undefined });

  const written = [];
  const wrappedWritable = new WrapWritableStream(
    new WritableStream({
      write(chunk) {
        written.push(chunk);
      }
    })
  );
  const writer = wrappedWritable.getWriter();

  await writer.write('write');
  await writer.close();

  assert.deepEqual(written, ['write']);
});
