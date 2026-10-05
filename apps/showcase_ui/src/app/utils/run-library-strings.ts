// QA-facing words for the run library and viewer. Glossary: computer, phone,
// run, recording, share link.

export const RUN_STRINGS = {
  title: 'Runs',
  searchLabel: 'Search runs',
  searchPlaceholder: 'Search by words or paste a run id',
  search: 'Search',
  moreFilters: 'More filters',
  loadMore: 'Load more',
  retry: 'Retry',
  clearFilters: 'Clear filters',
  emptyTitle: 'No runs yet.',
  emptyAction: 'Start one in Workspace',
  noMatch: 'No matching runs.',
  loadFailed: "Couldn't load runs. Your filters are kept.",
  catalogNotReady: 'The run library is getting ready. Try again in a minute.',
  backToRuns: 'Back to runs',
  viewerLoadFailed: "Couldn't load this run.",
  notFoundTitle: 'Run not found',
  notFoundBody: 'No run has this id. Check the link or search the library.',
  removedTitle: 'This run was removed',
  accessTitle: 'Sign in to open this run',
  accessBody: "Your sign-in may have expired. Sign in again and you'll come back to this run.",
  signIn: 'Sign in',
  ambiguousTitle: 'More than one run starts with this id',
  ambiguousBody: 'Pick the run you meant.',
  technicalDetails: 'Technical details',
  copyLink: 'Copy link',
  download: 'Download',
  pin: 'Pin',
  unpin: 'Unpin',
  delete: 'Delete',
  cancel: 'Cancel',
  checkAgain: 'Check again',
  startNewRun: 'Start new run with this prompt',
  linkCopied: 'Link copied.',
  preparingBundle: 'Preparing bundle…',
  downloadStarted: 'Download started.',
  downloadFailed: 'Download failed.',
  pinFailed: "Couldn't update the pin. Try again.",
  deleteFailed: "Couldn't delete this run. Try again."
} as const;

export const MEDIA_NOTICE =
  'Videos and screenshots are not redacted and may contain sensitive information.';
export const SHARE_NOTICE = "Anyone who passes this site's Cloudflare Access check can open this run.";
export const UNPIN_EXPIRED_NOTICE =
  'This run is past its retention date. Unpinning deletes it now.';
export const DELETE_NOTICE = 'Delete this run for everyone? This cannot be undone.';

export type Tone = 'ok' | 'warn' | 'danger' | 'neutral';

const OUTCOMES: Record<string, { label: string; icon: string; tone: Tone }> = {
  completed: { label: 'Passed', icon: 'check_circle', tone: 'ok' },
  success: { label: 'Passed', icon: 'check_circle', tone: 'ok' },
  failed: { label: 'Failed', icon: 'cancel', tone: 'danger' },
  interrupted: { label: 'Interrupted', icon: 'warning', tone: 'warn' },
  cancelled: { label: 'Cancelled', icon: 'block', tone: 'neutral' },
  running: { label: 'Running', icon: 'play_circle', tone: 'neutral' },
  paused: { label: 'Paused', icon: 'pause_circle', tone: 'neutral' },
  pending: { label: 'Queued', icon: 'schedule', tone: 'neutral' }
};

export function outcomeView(status: string | null | undefined) {
  return OUTCOMES[(status ?? '').toLowerCase()] ?? { label: 'Unknown', icon: 'help', tone: 'neutral' as Tone };
}

const INTERRUPT_REASONS: Record<string, string> = {
  host_disconnected: 'Your computer lost its connection to SmartQA.',
  bridge_closed: 'The browser tab holding the phone closed.',
  device_offline: 'The phone went offline.',
  server_restarted: 'SmartQA restarted.',
  auth_expired: "The computer's session expired."
};

export function interruptReason(reason: string | null | undefined): string {
  return (reason && INTERRUPT_REASONS[reason]) || 'The run stopped before it finished.';
}

export function interruptedSentence(step: number | null): string {
  const where = step ? `at step ${step}` : 'before the first step';
  return `Run interrupted ${where}. Recording is partial. Reconnecting will not resume this run.`;
}

const REMOVED_REASONS: Record<string, string> = {
  retention: 'It passed its retention date and was deleted automatically.',
  admin: 'An administrator deleted it.',
  session_deleted: 'It was deleted.'
};

export function removedReason(reason: string | null | undefined): string {
  return (reason && REMOVED_REASONS[reason]) || 'It was deleted.';
}

const WEEK = 7 * 86400;

/** "Expires on Oct 12" when a week or less remains; nothing earlier than that. */
export function expiresText(expiresAt: number | null | undefined, nowSeconds: number): string | null {
  if (expiresAt == null || expiresAt - nowSeconds > WEEK) return null;
  if (expiresAt <= nowSeconds) return 'Past retention date';
  return `Expires on ${new Date(expiresAt * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`;
}

export function truncate(text: string | null | undefined, max = 120): string {
  const clean = (text ?? '').replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean || 'Untitled run';
}
