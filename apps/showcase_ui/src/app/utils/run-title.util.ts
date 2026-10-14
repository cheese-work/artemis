export function runTitle(prompt: string | null | undefined): string {
  const line = prompt?.split(/\r?\n/).find(candidate => candidate.trim())?.trim() ?? '';
  return line
    .replace(/^(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)+/, '')
    .replace(/[*_`]/g, '')
    .replace(/\s+#+$/, '')
    .trim() || 'Untitled run';
}
