export interface WhatsNewEntry {
  id: string;
  date: string;
  title: string;
  body?: string;
}

export function parseWhatsNewEntries(value: unknown): WhatsNewEntry[] {
  if (!Array.isArray(value)) return [];

  return value.filter((entry): entry is WhatsNewEntry => {
    if (!entry || typeof entry !== 'object') return false;
    const candidate = entry as Record<string, unknown>;
    return typeof candidate['id'] === 'string'
      && candidate['id'].length > 0
      && typeof candidate['date'] === 'string'
      && typeof candidate['title'] === 'string'
      && (candidate['body'] === undefined || typeof candidate['body'] === 'string');
  });
}

export function hasUnseenWhatsNewEntries(
  entries: WhatsNewEntry[],
  lastSeenId: string | null
): boolean {
  if (entries.length === 0) return false;
  if (!lastSeenId) return true;

  const lastSeenIndex = entries.findIndex(entry => entry.id === lastSeenId);
  return lastSeenIndex === -1 || lastSeenIndex < entries.length - 1;
}

export function shouldAutoOpenWhatsNew(
  entries: WhatsNewEntry[],
  lastSeenId: string | null,
  isRunActive: boolean
): boolean {
  return !isRunActive && hasUnseenWhatsNewEntries(entries, lastSeenId);
}
