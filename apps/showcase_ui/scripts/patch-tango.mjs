import { readFile, writeFile } from 'node:fs/promises';

const packageJson = JSON.parse(
  await readFile(new URL('../node_modules/@yume-chan/stream-extra/package.json', import.meta.url), 'utf8')
);

if (packageJson.version !== '2.6.1') {
  throw new Error(`Expected @yume-chan/stream-extra 2.6.1, found ${packageJson.version}`);
}

const replacements = [
  {
    path: new URL('../node_modules/@yume-chan/stream-extra/esm/wrap-readable.js', import.meta.url),
    originalStart: 'start: async (controller) => {',
    patchedStart: 'start: (controller) => Promise.resolve().then(async () => {',
    originalEnd: '                this.#reader = this.readable.getReader();\n            },\n            pull:',
    patchedEnd: '                this.#reader = this.readable.getReader();\n            }),\n            pull:'
  },
  {
    path: new URL('../node_modules/@yume-chan/stream-extra/esm/wrap-writable.js', import.meta.url),
    originalStart: 'start: async () => {',
    patchedStart: 'start: () => Promise.resolve().then(async () => {',
    originalEnd: '                this.#writer = this.writable.getWriter();\n            },\n            write:',
    patchedEnd: '                this.#writer = this.writable.getWriter();\n            }),\n            write:'
  }
];

const updates = await Promise.all(
  replacements.map(async ({ path, originalStart, patchedStart, originalEnd, patchedEnd }) => {
    const source = await readFile(path, 'utf8');
    const alreadyPatched = source.includes(patchedStart) && source.includes(patchedEnd);

    if (alreadyPatched) {
      return null;
    }

    if (
      source.split(originalStart).length !== 2 ||
      source.split(originalEnd).length !== 2
    ) {
      throw new Error(`Unexpected @yume-chan/stream-extra source: ${path.pathname}`);
    }

    return { path, source: source.replace(originalStart, patchedStart).replace(originalEnd, patchedEnd) };
  })
);

for (const update of updates) {
  if (update) {
    await writeFile(update.path, update.source);
  }
}

console.info('Patched @yume-chan/stream-extra 2.6.1 Web Streams start callbacks');
