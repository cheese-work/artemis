import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, DestroyRef, InjectionToken, computed, inject, signal } from '@angular/core';
import { formatDate } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, fromEvent, interval, merge, of, startWith, switchMap } from 'rxjs';
import { BUILD_INFO } from '../../../build-info';
import { parseDeployVersion } from '../../core/models/deploy-version.model';

export interface BuildInfo {
  sha: string;
  builtAt: string;
}

/** The build bundled into this page by scripts/write-build-info.mjs (CHE-1411). */
export const PAGE_BUILD = new InjectionToken<BuildInfo>('PAGE_BUILD', { factory: () => BUILD_INFO });

const CHECK_INTERVAL_MS = 5 * 60_000;

@Component({
  selector: 'app-version-footer',
  standalone: true,
  template: `
    <footer class="version-footer" aria-label="SmartQA build">
      <button type="button" class="build" [title]="title" [attr.aria-label]="'Build ' + shortSha + ', copy full build id'" (click)="copy()">Build {{ shortSha }}</button>
      <span role="status" aria-live="polite">@if (feedback()) { · {{ feedback() }}}@if (newer()) { · New version available · }</span>
      @if (newer()) {<button type="button" class="reload" (click)="reload()">Reload</button>}
    </footer>
  `,
  styles: [`
    .version-footer {
      position: fixed;
      right: 10px;
      /* 2px + 14px line stays under the workspace composer (bottom: 18px). */
      bottom: 2px;
      z-index: 1;
      color: #475569;
      font: 11px 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
      line-height: 14px;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      pointer-events: none;
    }
    button {
      padding: 0;
      border: 0;
      color: inherit;
      background: none;
      font: inherit;
      cursor: pointer;
      pointer-events: auto;
    }
    .reload { text-decoration: underline; }
    button:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class VersionFooterComponent {
  private readonly page = inject(PAGE_BUILD);
  private readonly serverShortSha = signal<string | null>(null);
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;

  public readonly shortSha = this.page.sha.slice(0, 7);
  public readonly title = `Build ${this.page.sha}, built ${formatDate(this.page.builtAt, 'yyyy-MM-dd HH:mm', 'en-US', '+0700')} ICT`;
  public readonly feedback = signal('');
  public readonly newer = computed(() => {
    const server = this.serverShortSha();
    return !!server && this.page.sha !== 'unknown' && !this.page.sha.startsWith(server.toLowerCase());
  });

  constructor() {
    const http = inject(HttpClient);
    merge(fromEvent(window, 'focus'), interval(CHECK_INTERVAL_MS)).pipe(
      startWith(null),
      switchMap(() => http.get<unknown>('/api/system/version').pipe(catchError(() => of(null)))),
      takeUntilDestroyed()
    ).subscribe(value => this.serverShortSha.set(parseDeployVersion(value)?.shortSha ?? null));
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
