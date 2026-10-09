import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, DestroyRef, InjectionToken, computed, inject, signal } from '@angular/core';
import { DatePipe, formatDate } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, fromEvent, interval, merge, of, startWith, switchMap } from 'rxjs';
import { BUILD_INFO } from '../../../build-info';
import { DeployVersion, FULL_SHA, parseDeployVersion } from '../../core/models/deploy-version.model';

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
  imports: [DatePipe],
  template: `
    <footer class="version-footer" aria-label="SmartQA version">
      <span class="deployed">
        @if (version(); as v) {
          SmartQA @if (v.deployedAt) {
            <time [attr.datetime]="v.deployedAt">{{ v.deployedAt | date: 'yyyyMMdd-HHmm': '+0700' }}</time>
          } @else {
            {{ v.shortSha }}
          }
        } @else {
          SmartQA version unknown
        }
      </span> ·
      <button type="button" class="build" [title]="title" [attr.aria-label]="'Build ' + shortSha + ', copy full build id'" (click)="copy()">Build {{ shortSha }}</button>
      <span role="status" aria-live="polite">@if (feedback()) { · {{ feedback() }}}@if (newer()) { · New version available · }</span>
      @if (newer()) {<button type="button" class="reload" (click)="reload()">Reload</button>}
    </footer>
  `,
  styles: [`
    /* A strip in the app shell below the page area, never over page controls; above fixed page backdrops. */
    :host { display: block; flex: none; position: relative; z-index: 6; }
    .version-footer {
      padding: 2px 10px;
      border-top: 1px solid var(--color-rule);
      color: var(--color-text-muted);
      background: var(--color-surface);
      font: 11px var(--font-mono);
      line-height: 14px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }
    .deployed, [role="status"], button { white-space: nowrap; }
    button {
      min-height: var(--target);
      min-width: var(--target);
      padding: 0 var(--space-sm);
      border: 0;
      border-radius: var(--radius-md);
      color: inherit;
      background: var(--color-surface-subtle);
      font: inherit;
      cursor: pointer;
    }
    .reload { text-decoration: underline; }
    button:focus-visible { outline: 3px solid var(--focus-ring); outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class VersionFooterComponent {
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
    merge(fromEvent(window, 'focus'), interval(CHECK_INTERVAL_MS)).pipe(
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
