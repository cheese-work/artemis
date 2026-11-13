/**
 * Everything QA reads about computers and phones lives here, in one glossary:
 * Words: "computer" and "phone" only; scripts/lint-ui-strings.mjs enforces the banned word.
 */
import { ComputerStatus } from '../core/models/host.model';

export const COMPUTER_STRINGS = {
  tab: 'Computers',
  title: 'Computers',
  intro: 'Computers share their phones with SmartQA so runs can use them.',
  disabled: 'Computers are turned off on this server.',
  disabledHint: 'Ask an administrator to turn them on.',
  loading: 'Loading computers…',
  loadFailed: 'Computers could not be loaded. Check the server and try again.',
  empty: 'No computers connected yet.',
  connect: 'Connect a computer',
  dialogTitle: 'Connect a computer',
  codeOnce: 'This code is shown once. Copy the install message now.',
  copyMessage: 'Copy install message',
  copied: 'Copied',
  waiting: 'Waiting for the computer…',
  expired: 'This code expired. Create a new one.',
  used: 'This code was already used. Create a new one.',
  rateLimited: 'Too many tries. Wait a minute and try again.',
  adminOnly: 'Only administrators can connect, rename or remove a computer.',
  revoke: 'Revoke',
  rename: 'Rename',
  close: 'Close',
  confirmRevoke: 'Revoke computer',
  cancel: 'Cancel',
  noPhones: 'No phones found on this computer.',
  thisBrowser: 'This browser',
  aBrowser: 'A browser',
  browserOf: (owner: string) => `${owner}'s browser`
} as const;

const REASONS: Record<string, string> = {
  never_connected: 'Has not connected yet.',
  disconnected: 'The computer disconnected.',
  timeout: 'Lost its connection.',
  server_restarted: 'SmartQA restarted. Waiting for the computer to reconnect.',
  auth_expired: 'Its session expired. Restart the computer software to reconnect.',
  update_required: 'Needs a software update before it can connect.',
  revoked: 'Removed by an administrator.'
};

export function reasonText(reason: string | null): string {
  return (reason && REASONS[reason]) || 'Offline.';
}

export function offlineText(name: string, reason: string | null): string {
  switch (reason) {
    case 'timeout':
      return `${name} lost its connection.`;
    case 'disconnected':
      return `${name} disconnected.`;
    case 'auth_expired':
      return `${name}'s session expired. Restart its software to reconnect.`;
    case 'server_restarted':
      return `SmartQA restarted. Waiting for ${name} to reconnect.`;
    case 'never_connected':
      return `${name} has not connected yet.`;
    default:
      return `${name} is offline.`;
  }
}

export const STATUS_LABEL: Record<ComputerStatus, string> = {
  online: 'Online',
  reconnecting: 'Reconnecting…',
  offline: 'Offline',
  update_required: 'Update required',
  revoked: 'Revoked'
};

export function phonesSummary(shared: number, notShared: number): string {
  const phones = (n: number) => `${n} ${n === 1 ? 'phone' : 'phones'}`;
  return `${phones(shared)} shared, ${notShared} not shared`;
}

export function revokeWarning(name: string, runs: number): string {
  if (runs === 0) {
    return `Revoking ${name} disconnects it now. No runs are active on it.`;
  }
  return `Revoking ${name} disconnects it now and interrupts ${runs} active ${runs === 1 ? 'run' : 'runs'}.`;
}

export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

export function installMessage(origin: string, code: string, expiresAt: number): string {
  const expires = new Date(expiresAt * 1000).toISOString().slice(11, 16);
  return [
    'Connect this computer to SmartQA. Run in a terminal:',
    `curl -fsSL -H "X-Artemis-Enrollment-Code: ${code}" ${origin}/api/agent/install.sh | sh -s -- --code ${code}`,
    `The code works once and expires at ${expires} UTC.`
  ].join('\n');
}
