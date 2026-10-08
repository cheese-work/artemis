import {
  ReadableStream,
  WrapReadableStream,
  WritableStream,
  WrapWritableStream
} from '@yume-chan/stream-extra';

describe('Tango stream wrappers under Zone.js', () => {
  it('constructs and reads from a real wrapped readable stream', async () => {
    const wrapped = new WrapReadableStream(
      new ReadableStream<string>({
        start(controller) {
          controller.enqueue('read');
          controller.close();
        }
      })
    );
    const reader = wrapped.getReader();

    expect(await reader.read()).toEqual({ done: false, value: 'read' });
    expect(await reader.read()).toEqual({ done: true, value: undefined });
  });

  it('constructs and writes through a real wrapped writable stream', async () => {
    const written: string[] = [];
    const wrapped = new WrapWritableStream(
      new WritableStream<string>({
        write(chunk) {
          written.push(chunk);
        }
      })
    );
    const writer = wrapped.getWriter();

    await writer.write('write');
    await writer.close();

    expect(written).toEqual(['write']);
  });
});
