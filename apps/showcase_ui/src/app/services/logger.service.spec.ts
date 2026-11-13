import { TestBed } from '@angular/core/testing';
import { LoggerService, redact } from './logger.service';

describe('LoggerService', () => {
  afterEach(() => localStorage.removeItem('artemis.log'));

  it('redacts reusable summaries, tool values and error messages', () => {
    expect(redact('password="summary-sentinel" Bearer abcdefghijklmnop')).not.toContain('summary-sentinel');
    expect(JSON.stringify(redact({ api_key: 'object-sentinel', args: { text: 'typed-sentinel' } }))).not.toContain('sentinel');
    expect(JSON.stringify(redact(new Error('password=error-sentinel')))).not.toContain('error-sentinel');
  });

  it('defaults to warnings and errors without swallowing failures', () => {
    const service = TestBed.inject(LoggerService);
    const debug = spyOn(console, 'debug');
    const error = spyOn(console, 'error');
    service.debug('debug-hidden');
    service.error('failed', new Error('password=error-sentinel'));
    expect(debug).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalled();
    expect(JSON.stringify(error.calls.mostRecent().args)).not.toContain('error-sentinel');
  });

  it('reads the level from localStorage on each call', () => {
    const service = TestBed.inject(LoggerService);
    const debug = spyOn(console, 'debug');
    localStorage.setItem('artemis.log', 'debug');
    service.debug('password=debug-sentinel');
    expect(debug).toHaveBeenCalled();
    expect(JSON.stringify(debug.calls.mostRecent().args)).not.toContain('debug-sentinel');
    localStorage.setItem('artemis.log', 'error');
    service.debug('hidden');
    expect(debug.calls.count()).toBe(1);
  });

  it('reports unavailable browser storage and still logs errors', () => {
    spyOn(Storage.prototype, 'getItem').and.throwError('storage blocked');
    const warning = spyOn(console, 'warn');
    const error = spyOn(console, 'error');
    TestBed.inject(LoggerService).error('failure');
    expect(warning).toHaveBeenCalled();
    expect(error).toHaveBeenCalled();
  });
});
