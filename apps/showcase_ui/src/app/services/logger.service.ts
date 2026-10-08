import { Injectable, inject } from '@angular/core';
import { BrowserStorageService } from './browser-storage.service';

type LogLevel = 'debug' | 'info' | 'warn' | 'error';
const LEVELS: LogLevel[] = ['debug', 'info', 'warn', 'error'];
const SENSITIVE_KEY = /(?:password|passwd|pwd|passcode|secret|token|api[_-]?key|authorization|cookie|goal|initial_goal|args|arguments|typed_text|input_text)$/i;

export function redact(value: string): string;
export function redact(value: unknown): unknown;
export function redact(value: unknown): unknown {
  if (value instanceof Error) {
    return { name: redact(value.name), message: redact(value.message), stack: redact(value.stack) };
  }
  if (Array.isArray(value)) {
    return value.map(item => redact(item));
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      key, SENSITIVE_KEY.test(key) ? '[REDACTED]' : redact(item)
    ]));
  }
  if (typeof value !== 'string') {
    return value;
  }
  return value
    .replace(/(\b[\w.-]*(?:password|passwd|pwd|passcode|secret|token|api[_-]?key|authorization)\b["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|(?:bearer\s+)?[^\s,;&}\]]+)/gi, '$1[REDACTED]')
    .replace(/(\b(?:set-)?cookie\s*[:=]\s*)[^\r\n]+/gi, '$1[REDACTED]')
    .replace(/(\bbearer\s+)[A-Za-z0-9._~+/=-]+/gi, '$1[REDACTED]')
    .replace(/\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,})/g, '[REDACTED]')
    .replace(/(\b(?:type|typed|enter|entered|input)\s+(?:the\s+)?(?:password|passcode|pin)\s+(?:is\s+|as\s+)?)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, '$1[REDACTED]')
    .replace(/(\b(?:password|passcode)\s+(?:is|was)\s*[:=]?\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)/gi, '$1[REDACTED]');
}

@Injectable({ providedIn: 'root' })
export class LoggerService {
  private readonly browserStorage = inject(BrowserStorageService);
  debug(message: string, ...details: unknown[]): void { this.log('debug', message, details); }
  info(message: string, ...details: unknown[]): void { this.log('info', message, details); }
  warn(message: string, ...details: unknown[]): void { this.log('warn', message, details); }
  error(message: string, ...details: unknown[]): void { this.log('error', message, details); }

  private log(level: LogLevel, message: string, details: unknown[]): void {
    let configured = 'warn';
    try {
      configured = this.browserStorage.getItem('artemis.log') || configured;
    } catch (error) {
      console.warn('Logger settings are unavailable:', redact(error));
    }
    const minimum = LEVELS.includes(configured as LogLevel) ? configured as LogLevel : 'warn';
    if (LEVELS.indexOf(level) >= LEVELS.indexOf(minimum)) {
      try {
        console[level](redact(message), ...details.map(item => redact(item)));
      } catch {
        console.error('[REDACTED: unrenderable log event]');
      }
    }
  }
}
