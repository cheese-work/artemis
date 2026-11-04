export type SearchIntent =
  | { kind: 'empty' }
  | { kind: 'jump'; id: string }
  | { kind: 'text'; q: string };

const FULL_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHORT_ID = /^[0-9a-f]{8}$/;

/**
 * What the search box means. A full run id or its 8-character prefix jumps to
 * the run; everything else is plain text, sent as typed. The server quotes it
 * into FTS5 terms, so the client never builds or exposes search syntax.
 */
export function classifySearch(raw: string): SearchIntent {
  const text = raw.trim();
  if (!text) return { kind: 'empty' };
  const id = text.toLowerCase();
  return FULL_ID.test(id) || SHORT_ID.test(id) ? { kind: 'jump', id } : { kind: 'text', q: text };
}
