import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { StorageReport } from '../../core/models/storage.model';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';
import { StorageService } from '../../services/storage.service';
import { StorageComponent } from './storage.component';

const report = (over: Partial<StorageReport> = {}): StorageReport => ({
  usage_bytes: 3 * 1024 ** 3,
  run_count: 12,
  clearable_count: 9,
  pinned_count: 3,
  pinned_bytes: 1024 ** 3,
  disk: { total_bytes: 100 * 1024 ** 3, free_bytes: 60 * 1024 ** 3, free_percent: 60 },
  warnings: [],
  retention: { enabled: false, days: 30 },
  ...over
});

describe('StorageComponent', () => {
  let storage: jasmine.SpyObj<StorageService>;
  let adminConfig: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<StorageComponent>;
  const admin: AdminIdentity = { email: 'a@x.test', admin: true, auth_mode: 'cloudflare', reason: null };
  const member: AdminIdentity = { email: 'q@x.test', admin: false, auth_mode: 'cloudflare', reason: 'not_on_allowlist' };

  beforeEach(async () => {
    storage = jasmine.createSpyObj<StorageService>('StorageService', ['report', 'clearAll']);
    adminConfig = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    adminConfig.getIdentity.and.returnValue(of(admin));
    storage.report.and.returnValue(of(report()));
    await TestBed.configureTestingModule({
      imports: [StorageComponent],
      providers: [
        { provide: StorageService, useValue: storage },
        { provide: AdminConfigService, useValue: adminConfig }
      ]
    }).compileComponents();
  });

  function create(): HTMLElement {
    fixture = TestBed.createComponent(StorageComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  const text = (root: HTMLElement) => root.textContent?.replace(/\s+/g, ' ') ?? '';

  it('shows usage, run count, pinned total and free disk', () => {
    const root = create();
    expect(text(root)).toContain('12 runs');
    expect(text(root)).toContain('3 pinned');
    expect(text(root)).toContain('3.0 GB');
    expect(text(root)).toContain('60% free');
    const bar = root.querySelector('[role="meter"]') as HTMLElement;
    expect(bar.getAttribute('aria-valuenow')).toBe('40');
    expect(root.querySelector('.warning')).toBeNull();
  });

  it('says what the warning is, in words, below 20% and below 10% free and on a 507', () => {
    storage.report.and.returnValue(
      of(
        report({
          disk: { total_bytes: 100, free_bytes: 5, free_percent: 5 },
          warnings: [
            { level: 'critical', code: 'low_disk', message: 'Only 5.0% of the disk is free.' },
            { level: 'critical', code: 'insufficient_storage', message: 'A write failed for lack of disk space.' }
          ]
        })
      )
    );
    const root = create();
    const warnings = Array.from(root.querySelectorAll('.warning')).map((n) => text(n as HTMLElement));
    expect(warnings.length).toBe(2);
    expect(warnings[0]).toContain('Critical');
    expect(warnings[0]).toContain('Only 5.0% of the disk is free.');
    expect(warnings[1]).toContain('A write failed for lack of disk space.');
  });

  it('shows a warning level that is not only a colour', () => {
    storage.report.and.returnValue(
      of(report({ warnings: [{ level: 'warning', code: 'low_disk', message: 'Only 15.0% of the disk is free.' }] }))
    );
    expect(text(create())).toContain('Warning');
  });

  it('offers Clear all only to admins', () => {
    expect(create().querySelector('button.clear-all')).not.toBeNull();
    fixture.destroy();
    adminConfig.getIdentity.and.returnValue(of(member));
    expect(create().querySelector('button.clear-all')).toBeNull();
  });

  it('asks for the exact count before clearing, and refuses a wrong one', () => {
    const root = create();
    (root.querySelector('button.clear-all') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(text(root)).toContain('9 runs for everyone');
    const input = root.querySelector('input.confirm-count') as HTMLInputElement;
    const confirm = () => root.querySelector('button.confirm-clear') as HTMLButtonElement;
    input.value = '8';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(confirm().disabled).toBeTrue();
    input.value = '9';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(confirm().disabled).toBeFalse();
    expect(storage.clearAll).not.toHaveBeenCalled();
  });

  it('sends the typed count, then reloads the numbers', () => {
    storage.clearAll.and.returnValue(of({ deleted: ['a'], deferred: [] }));
    const root = create();
    (root.querySelector('button.clear-all') as HTMLButtonElement).click();
    fixture.detectChanges();
    const input = root.querySelector('input.confirm-count') as HTMLInputElement;
    input.value = '9';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    (root.querySelector('button.confirm-clear') as HTMLButtonElement).click();
    expect(storage.clearAll).toHaveBeenCalledOnceWith(9);
    expect(storage.report).toHaveBeenCalledTimes(2);
  });

  it('shows the server count when it changed under the admin', () => {
    storage.clearAll.and.returnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { error: 'count_mismatch', expected_count: 10 } }))
    );
    const root = create();
    (root.querySelector('button.clear-all') as HTMLButtonElement).click();
    fixture.detectChanges();
    const input = root.querySelector('input.confirm-count') as HTMLInputElement;
    input.value = '9';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    (root.querySelector('button.confirm-clear') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(text(root)).toContain('10 runs now');
  });

  it('offers Retry when the report cannot be read', () => {
    storage.report.and.returnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    const root = create();
    expect(text(root)).toContain('Couldn’t read storage');
    storage.report.and.returnValue(of(report()));
    (root.querySelector('button.retry') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(text(root)).toContain('12 runs');
  });
});
