// PR check: a change to the SmartQA UI must add a What's New entry, unless the
// PR carries the `no-whats-new` label.
// Usage: PR_LABELS='["label"]' node check-whats-new-entry.mjs <base-sha> <head-sha>
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const SKIP_LABEL = 'no-whats-new';
const ENTRIES = 'apps/showcase_ui/whats-new/entries/';

/** `changes` are `git diff --name-status --no-renames` lines. */
export function checkWhatsNewEntry(changes, labels) {
  const parsed = changes.filter(Boolean).map(line => line.split('\t'));
  const touchesUi = parsed.some(([, file]) => file.startsWith('apps/showcase_ui/src/') && !file.endsWith('.spec.ts'));
  const addsEntry = parsed.some(([status, file]) => status === 'A' && file.startsWith(ENTRIES) && file.endsWith('.json'));
  if (!touchesUi || addsEntry || labels.includes(SKIP_LABEL)) return { ok: true };
  return {
    ok: false,
    message: [
      'This PR changes the SmartQA UI (apps/showcase_ui/src) but adds no What\'s New entry.',
      `Add one file: ${ENTRIES}<YYYY-MM-DD>-<slug>.json`,
      '  {"id": "<file name without .json>", "date": "<YYYY-MM-DD>", "title": "<one plain sentence>", "body": "<optional, at most 3 short lines>", "issues": ["CHE-1234"]}',
      'Write for QAs: plain language, no internal detail. See CONTRIBUTING.md, "What\'s New entries".',
      `If QAs will not notice this change (refactor, tests only, internal tooling), add the label "${SKIP_LABEL}" instead.`,
    ].join('\n'),
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [base, head] = process.argv.slice(2);
  if (!base || !head) {
    console.error('Usage: node check-whats-new-entry.mjs <base-sha> <head-sha>');
    process.exit(2);
  }
  const changes = execFileSync('git', ['diff', '--name-status', '--no-renames', `${base}...${head}`], { encoding: 'utf8' }).split('\n');
  const result = checkWhatsNewEntry(changes, JSON.parse(process.env.PR_LABELS || '[]'));
  if (!result.ok) {
    console.error(result.message);
    process.exit(1);
  }
  console.log('What\'s New entry check passed.');
}
