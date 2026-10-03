export interface WhatsNewEntry {
  id: string;
  date: string;
  title: string;
  body?: string;
}

function isWhatsNewEntry(value: unknown): value is WhatsNewEntry {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate['id'] !== 'string' || !candidate['id'].trim()
    || typeof candidate['date'] !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(candidate['date'])
    || typeof candidate['title'] !== 'string' || !candidate['title'].trim()
    || (candidate['body'] !== undefined && typeof candidate['body'] !== 'string')) {
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

export function hasUnseenWhatsNewEntries(
  entries: WhatsNewEntry[],
  lastSeenId: string | null
): boolean {
  if (entries.length === 0) return false;
  if (!lastSeenId) return true;

  return entries[0].id !== lastSeenId;
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
