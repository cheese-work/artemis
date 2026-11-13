import { Component, ChangeDetectionStrategy, DestroyRef, OnInit, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { HttpErrorResponse } from '@angular/common/http';
import { StorageReport } from '../../core/models/storage.model';
import { AdminConfigService } from '../../services/admin-config.service';
import { StorageService } from '../../services/storage.service';

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

/** 1536 -> "1.5 KB"; one decimal above bytes. */
export function formatBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit]}`;
}

@Component({
  selector: 'app-storage',
  standalone: true,
  templateUrl: './storage.component.html',
  styleUrl: './storage.component.scss',
  changeDetection: ChangeDetectionStrategy.Eager
})
export class StorageComponent implements OnInit {
  private readonly storage = inject(StorageService);
  private readonly adminConfig = inject(AdminConfigService);
  private readonly destroyRef = inject(DestroyRef);

  public readonly formatBytes = formatBytes;
  public readonly report = signal<StorageReport | null>(null);
  public readonly loading = signal(true);
  public readonly failed = signal(false);
  public readonly isAdmin = signal(false);
  public readonly clearing = signal(false);
  public readonly typedCount = signal('');
  public readonly clearError = signal('');
  public readonly usedPercent = computed(() => {
    const current = this.report();
    return current ? Math.round(100 - current.disk.free_percent) : 0;
  });
  public readonly countMatches = computed(() => {
    const current = this.report();
    return current !== null && this.typedCount().trim() === String(current.clearable_count);
  });

  public ngOnInit(): void {
    this.adminConfig
      .getIdentity()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ next: (identity) => this.isAdmin.set(identity.admin), error: () => this.isAdmin.set(false) });
    this.load();
  }

  public load(): void {
    this.loading.set(true);
    this.failed.set(false);
    this.storage
      .report()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (report) => {
          this.report.set(report);
          this.loading.set(false);
        },
        error: () => {
          this.failed.set(true);
          this.loading.set(false);
        }
      });
  }

  public askClear(): void {
    this.typedCount.set('');
    this.clearError.set('');
    this.clearing.set(true);
  }

  public cancelClear(): void {
    this.clearing.set(false);
  }

  public confirmClear(): void {
    const current = this.report();
    if (!current || !this.countMatches()) return;
    this.storage.clearAll(current.clearable_count).subscribe({
      next: () => {
        this.clearing.set(false);
        this.load();
      },
      error: (err: HttpErrorResponse) => {
        const expected = err.error?.expected_count;
        this.clearError.set(
          typeof expected === 'number'
            ? `The count changed: ${expected} runs now. Reload the numbers and try again.`
            : 'Could not clear runs. Only an admin can do this.'
        );
      }
    });
  }
}
