import { ChangeDetectionStrategy, Component, Input, inject } from '@angular/core';
import { DatePipe } from '@angular/common';
import { VersionInfoService } from '../../services/version-info.service';
export { PAGE_BUILD } from '../../services/version-info.service';
export type { BuildInfo } from '../../services/version-info.service';

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
      <span role="status" aria-live="polite">@if (feedback()) { · {{ feedback() }}}@if (showNotice && newer()) { · New version available · }</span>
      @if (showNotice && newer()) {<button type="button" class="reload" (click)="reload()">Reload</button>}
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
    :host(.account-version) .version-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 0 4px; padding: 8px 0 0; font-size: 12px; text-align: left; }
    :host(.account-version) button { min-width: 44px; min-height: 44px; padding: 0 4px; }
    button:focus-visible { outline: 3px solid var(--focus-ring); outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class VersionFooterComponent {
  @Input() public showNotice = true;
  private readonly info = inject(VersionInfoService);
  public readonly version = this.info.version;
  public readonly shortSha = this.info.shortSha;
  public readonly title = this.info.title;
  public readonly feedback = this.info.feedback;
  public readonly newer = this.info.newer;

  public copy(): Promise<void> { return this.info.copy(); }

  public reload(): void {
    this.info.reload();
  }
}
