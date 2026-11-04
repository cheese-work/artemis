export interface WhatsNewEntry {
  id: string;
  date: string;
  title: string;
  body?: string;
  /** Related issue numbers, shown as plain text, for example `CHE-1334`. */
  issues?: string[];
}

function isWhatsNewEntry(value: unknown): value is WhatsNewEntry {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate['id'] !== 'string' || !candidate['id'].trim()
    || typeof candidate['date'] !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(candidate['date'])
    || typeof candidate['title'] !== 'string' || !candidate['title'].trim()
    || (candidate['body'] !== undefined && typeof candidate['body'] !== 'string')
    || (candidate['issues'] !== undefined && (!Array.isArray(candidate['issues'])
      || !candidate['issues'].every(issue => typeof issue === 'string' && /^CHE-\d+$/.test(issue))))) {
    return false;
  }

  const parsedDate = new Date(`${candidate['date']}T00:00:00.000Z`);
  return !Number.isNaN(parsedDate.valueOf()) && parsedDate.toISOString().slice(0, 10) === candidate['date'];
}

export function parseWhatsNewEntries(value: unknown): WhatsNewEntry[] {
  if (!Array.isArray(value) || !value.every(isWhatsNewEntry)) return [];

  const entries = value as WhatsNewEntry[];
  const ids = new Set(entries.map(entry => entry.id));
  if (ids.size !== entries.length) return [];
  if (entries.some((entry, index) => index > 0 && entry.date > entries[index - 1].date)) return [];
  return entries;
}

/**
 * What the reader has seen: the ids of every entry on the newest date. Several
 * PRs can ship entries on one day, and a later one may sort below an earlier
 * one; this key still changes. With one entry that day it is just its id.
 */
export function whatsNewSeenKey(entries: WhatsNewEntry[]): string | null {
  if (entries.length === 0) return null;
  return entries.filter(entry => entry.date === entries[0].date).map(entry => entry.id).join(' ');
}

export function hasUnseenWhatsNewEntries(
  entries: WhatsNewEntry[],
  lastSeenId: string | null
): boolean {
  if (entries.length === 0) return false;
  if (!lastSeenId) return true;

  return whatsNewSeenKey(entries) !== lastSeenId;
}

export function shouldAutoOpenWhatsNew(
  entries: WhatsNewEntry[],
  lastSeenId: string | null,
  isRunActive: boolean,
  hasPromptDraft = false,
  hasError = false
): boolean {
  return !isRunActive && !hasPromptDraft && !hasError && hasUnseenWhatsNewEntries(entries, lastSeenId);
}
