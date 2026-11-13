/** Longest text shown inline before a report is folded behind a disclosure. */
export const REPORT_SUMMARY_MAX = 280;
/** Longest full report that is still safe to repeat in a tooltip. */
export const REPORT_TITLE_MAX = 1000;

const KEY_LABEL = /^(?:[-*>]\s*)?\*\*[^*\n]*(kết quả|result|conclusion|summary|tóm tắt)[^*\n]*\*\*/i;
const HEADING = /^\s{0,3}#{1,6}\s/;
const TABLE_ROW = /^\s*\|/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const FENCE = /^\s*```/;

/** True for a report too long or too structured to read as one inline line. */
export function isLongReport(text: string): boolean {
  if (text.length > REPORT_SUMMARY_MAX) return true;
  return text.includes('\n') && text.split('\n').some((line) => HEADING.test(line) || TABLE_ROW.test(line));
}

function stripMarkdown(line: string): string {
  return line
    .replace(/^\s*>\s?/, '')
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\*\*(.+?)\*\*|(?<!\w)__(.+?)__(?!\w)/g, '$1$2')
    .replace(/\*(?=\S)(.+?)(?<=\S)\*|(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)/g, '$1$2')
    .replace(/~~(.+?)~~/g, '$1')
    .replace(/`([^`]*)`/g, '$1');
}

interface Block {
  heading: boolean;
  text: string;
}

const KEY_WORDS = /kết quả|result|conclusion|summary|tóm tắt/i;
/** A bold result label alone on its line, e.g. `**Kết quả chính:**`. */
const LABEL_ONLY = /^(?:[-*>]\s*)?\*\*[^*\n]*\*\*\s*:?\s*$/;
const HEADING_TEXT = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;

/** Blocks of prose and headings: tables, rules and code fences are dropped. */
function proseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let lines: string[] = [];
  const flush = () => {
    if (lines.length) blocks.push({ heading: false, text: lines.join('\n') });
    lines = [];
  };
  let inFence = false;
  for (const line of text.replace(/\r\n?/g, '\n').split('\n')) {
    if (FENCE.test(line)) {
      inFence = !inFence;
    } else if (inFence || TABLE_ROW.test(line) || RULE.test(line)) {
      continue;
    } else if (HEADING.test(line)) {
      flush();
      blocks.push({ heading: true, text: line.replace(HEADING_TEXT, '$1') });
    } else if (line.trim() === '') {
      flush();
    } else {
      lines.push(line.trim());
    }
  }
  flush();
  return blocks;
}

function collapse(text: string): string {
  return stripMarkdown(text).replace(/\s+/g, ' ').trim();
}

function withLabel(label: string, content: string): string {
  return `${collapse(label).replace(/:$/, '')}: ${content}`;
}

/** Text of the first key-result block: its own text, or the prose that follows a bare label or heading. */
function keyResult(blocks: Block[]): string | null {
  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i];
    const text = block.text.normalize('NFC');
    const isKey = block.heading ? KEY_WORDS.test(text) : KEY_LABEL.test(text);
    if (!isKey) continue;
    const bareLabel = block.heading || (!text.includes('\n') && LABEL_ONLY.test(text));
    if (!bareLabel) return block.text;
    const next = blocks[i + 1];
    if (next && !next.heading) return withLabel(block.text, next.text.split('\n').map(collapse).join(' '));
  }
  return null;
}

/** Short plain-text summary of a markdown report: the key-result content, else the first prose paragraph. */
export function summarizeReport(text: string, max = REPORT_SUMMARY_MAX): string {
  const blocks = proseBlocks(text);
  const chosen = keyResult(blocks) ?? blocks.find((b) => !b.heading)?.text ?? text;
  const plain = chosen.split('\n').map(collapse).join(' ').replace(/\s+/g, ' ').trim();
  return plain.length > max ? `${plain.slice(0, max - 1).trimEnd()}…` : plain;
}
