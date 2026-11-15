import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDirectory = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repositoryRoot = path.resolve(appDirectory, '../..');
const sourceFile = path.join(repositoryRoot, 'docs/smartqa-verdicts.md');
const helpRoute = JSON.parse(await readFile(path.join(appDirectory, 'src/app/core/smartqa-help-route.json'), 'utf8'));
const outputIndex = process.argv.indexOf('--output');
const outputFile = outputIndex === -1
  ? path.join(appDirectory, 'public', helpRoute.path.replace(/^\//, ''))
  : path.resolve(process.cwd(), process.argv[outputIndex + 1]);

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
}

function inlineMarkdown(value) {
  return escapeHtml(value).replace(/`([^`]+)`/g, '<code>$1</code>');
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const output = [];
  let paragraph = [];
  let list = [];
  let code = null;

  const flushParagraph = () => {
    if (paragraph.length) output.push(`<p>${inlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (list.length) output.push(`<ul>${list.map(item => `<li>${inlineMarkdown(item)}</li>`).join('')}</ul>`);
    list = [];
  };

  for (const line of lines) {
    if (line.startsWith('```')) {
      flushParagraph();
      flushList();
      if (code === null) code = [];
      else {
        output.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
        code = null;
      }
      continue;
    }
    if (code !== null) {
      code.push(line);
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    const item = /^-\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      const title = heading[2];
      const id = /^[a-z0-9_]+$/.test(title) ? ` id="${title}"` : '';
      output.push(`<h${level}${id}>${inlineMarkdown(title)}</h${level}>`);
    } else if (item) {
      flushParagraph();
      list.push(item[1]);
    } else if (!line.trim()) {
      flushParagraph();
      flushList();
    } else {
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
  flushList();
  return output.join('\n');
}

const markdown = await readFile(sourceFile, 'utf8');
const body = renderMarkdown(markdown);
const page = `<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SmartQA verdict results</title>
  <style>
    :root { color-scheme: light dark; font: 16px/1.6 system-ui, sans-serif; }
    body { max-width: 72rem; margin: 0 auto; padding: 2rem 1rem 5rem; }
    h1, h2, h3 { line-height: 1.25; margin-block: 2rem 0.75rem; }
    p, li { max-width: 72ch; }
    code, pre { font-family: ui-monospace, monospace; }
    code { overflow-wrap: anywhere; }
    pre { overflow-x: auto; padding: 1rem; border: 1px solid #8886; border-radius: 0.5rem; }
    a { min-height: 44px; }
  </style>
</head>
<body>
<main>
${body}
</main>
</body>
</html>
`;

await mkdir(path.dirname(outputFile), { recursive: true });
await writeFile(outputFile, page);
console.log(`Rendered canonical SmartQA guidance to ${path.relative(process.cwd(), outputFile)}.`);
