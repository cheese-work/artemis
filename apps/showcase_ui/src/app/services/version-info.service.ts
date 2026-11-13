import { HttpClient } from '@angular/common/http';
import { formatDate } from '@angular/common';
import { DestroyRef, Injectable, InjectionToken, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, fromEvent, interval, merge, of, startWith, switchMap } from 'rxjs';
import { BUILD_INFO } from '../../build-info';
import { DeployVersion, FULL_SHA, parseDeployVersion } from '../core/models/deploy-version.model';

export interface BuildInfo {
  sha: string;
  builtAt: string;
}

export const PAGE_BUILD = new InjectionToken<BuildInfo>('PAGE_BUILD', { factory: () => BUILD_INFO });

@Injectable({ providedIn: 'root' })
export class VersionInfoService {
  private readonly page = inject(PAGE_BUILD);
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;

  public readonly version = signal<DeployVersion | null>(null);
  public readonly shortSha = this.page.sha.slice(0, 7);
  public readonly title = `Build ${this.page.sha}, built ${formatDate(this.page.builtAt, 'yyyy-MM-dd HH:mm', 'en-US', '+0700')} ICT`;
  public readonly feedback = signal('');
  public readonly newer = computed(() => {
    const server = this.version()?.sha;
    return !!server && FULL_SHA.test(this.page.sha) && this.page.sha.toLowerCase() !== server;
  });

  constructor() {
    const http = inject(HttpClient);
    merge(fromEvent(window, 'focus'), interval(5 * 60_000)).pipe(
      startWith(null),
      switchMap(() => http.get<unknown>('/api/system/version').pipe(catchError(() => of(null)))),
      takeUntilDestroyed()
    ).subscribe(value => this.version.set(parseDeployVersion(value)));
    inject(DestroyRef).onDestroy(() => {
      if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    });
  }

  public async copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.page.sha);
      this.feedback.set('Build copied');
    } catch {
      this.feedback.set('Copy failed');
    }
    if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    this.feedbackTimer = setTimeout(() => this.feedback.set(''), 1600);
  }

  public reload(): void {
    location.reload();
  }
}
