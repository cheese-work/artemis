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

/** Paragraphs of prose: headings, tables, rules and code fences are dropped. */
function proseParagraphs(text: string): string[] {
  const paragraphs: string[][] = [[]];
  let inFence = false;
  for (const line of text.replace(/\r\n?/g, '\n').split('\n')) {
    if (FENCE.test(line)) {
      inFence = !inFence;
    } else if (inFence || HEADING.test(line) || TABLE_ROW.test(line) || RULE.test(line)) {
      continue;
    } else if (line.trim() === '') {
      paragraphs.push([]);
    } else {
      paragraphs[paragraphs.length - 1].push(line.trim());
    }
  }
  return paragraphs.filter((lines) => lines.length).map((lines) => lines.join('\n'));
}

function collapse(text: string): string {
  return stripMarkdown(text).replace(/\s+/g, ' ').trim();
}

/** Short plain-text summary of a markdown report: the key-result paragraph, else the first prose paragraph. */
export function summarizeReport(text: string, max = REPORT_SUMMARY_MAX): string {
  const paragraphs = proseParagraphs(text);
  const chosen = paragraphs.find((p) => KEY_LABEL.test(p.normalize('NFC'))) ?? paragraphs[0] ?? text;
  const plain = chosen.split('\n').map(collapse).join(' ').replace(/\s+/g, ' ').trim();
  return plain.length > max ? `${plain.slice(0, max - 1).trimEnd()}…` : plain;
}
