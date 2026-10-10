import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

@Component({
  selector: '[appRunStepRow]',
  standalone: true,
  template: `
    @if (presentation() === 'phase') {
      <span class="phase-worked-time">{{ title() }} for {{ duration() }}s</span>
    } @else {
      <span class="step-icon material-symbols-outlined" aria-hidden="true">{{ failed() ? 'error' : icon() }}</span>
      <span class="step-thumbnail" aria-hidden="true">
        @if (thumbnail(); as image) {
          @if (failedThumbnail() !== image) {
            <img [src]="image" alt="" loading="lazy" (error)="failedThumbnail.set(image)">
          } @else { <span class="material-symbols-outlined">image_not_supported</span> }
        } @else { <span class="material-symbols-outlined">image</span> }
      </span>
      <span class="step-summary">
        <span class="step-heading"><span class="step-number">Step {{ stepNumber() }}</span><span class="step-title">{{ title() }}</span></span>
        <span class="step-meta">
          @if (failed()) { <span class="step-failed">Failed</span> }
          @if (kind()) { <span class="step-kind">{{ kind() }}</span> }
          @if (failureDetail()) { <span class="step-failure-detail" [attr.title]="failureTitle()">{{ failureDetail() }}</span> }
        </span>
      </span>
      @if (duration() !== null || time() !== null) { <span class="step-duration">{{ duration() ?? time() }}</span> }
    }
    <ng-content />
  `,
  host: {
    '[class.timeline-step]': "presentation() === 'step'",
    '[class.failed]': 'failed()'
  },
  styles: [`
    :host(.timeline-step) { display: grid; grid-template-columns: 20px 56px minmax(0, 1fr) auto; align-items: center; gap: 8px; height: 56px; box-sizing: border-box; }
    .phase-worked-time { color: var(--color-text-muted); font-weight: normal; }
    .step-icon { font-size: 20px; }
    .step-thumbnail { display: flex; align-items: center; justify-content: center; width: 56px; height: 40px; overflow: hidden; border-radius: var(--radius-sm); color: var(--color-text-muted); background: var(--color-surface-subtle); }
    .step-thumbnail img { display: block; width: 100%; height: 100%; object-fit: contain; }
    .step-summary, .step-title, .step-kind, .step-failure-detail { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
    .step-heading, .step-meta { display: flex; align-items: center; gap: 6px; min-width: 0; }
    .step-title { font-size: 13px; }
    .step-number, .step-meta { font-size: 12px; color: var(--color-text-muted); }
    .step-number, .step-failed, .step-duration { flex-shrink: 0; }
    .step-duration { font: 12px var(--font-mono); color: var(--color-text-muted); white-space: nowrap; }
    .step-failed, :host(.failed) .step-icon { color: var(--status-danger-fg); }
    .step-failed { display: inline-flex; font-weight: 600; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunStepRowComponent {
  readonly presentation = input<'step' | 'phase'>('step');
  readonly title = input.required<string>();
  readonly stepNumber = input<number | null>(null);
  readonly failed = input(false);
  readonly icon = input('ads_click');
  readonly thumbnail = input<string | null>(null);
  readonly kind = input<string | null>(null);
  readonly time = input<string | null>(null);
  readonly duration = input<number | string | null>(null);
  readonly failedThumbnail = signal<string | null>(null);
  readonly failureDetail = input<string | null>(null);
  /** Full text for the tooltip when failureDetail is only a summary. */
  readonly failureTitle = input<string | null>(null);
}
