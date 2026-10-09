import { ChangeDetectionStrategy, Component, input } from '@angular/core';

@Component({
  selector: '[appRunStepRow]',
  standalone: true,
  template: `
    @if (presentation() === 'phase') {
      <span class="phase-worked-time">{{ title() }} for {{ duration() }}s</span>
    } @else {
      <span class="step-number">Step {{ stepNumber() }}</span>
      <span class="step-title">{{ title() }}</span>
      @if (failed()) {
        <span class="step-failed"><span class="material-symbols-outlined" aria-hidden="true">error</span> Failed</span>
      }
      @if (duration() !== null) { <span class="step-duration">{{ duration() }}</span> }
      @if (failureDetail()) { <span class="step-failure-detail" [attr.title]="failureTitle()">{{ failureDetail() }}</span> }
    }
    <ng-content />
  `,
  styles: [`
    .phase-worked-time { color: var(--color-text-muted); font-weight: normal; }
    .step-number { font-size: 12px; color: var(--evidence-muted); }
    .step-failure-detail { flex: 1 1 100%; min-width: 0; max-width: 100%; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
    .step-failed { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 600; color: var(--status-danger-fg); }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunStepRowComponent {
  readonly presentation = input<'step' | 'phase'>('step');
  readonly title = input.required<string>();
  readonly stepNumber = input<number | null>(null);
  readonly failed = input(false);
  readonly duration = input<number | string | null>(null);
  readonly failureDetail = input<string | null>(null);
  /** Full text for the tooltip when failureDetail is only a summary. */
  readonly failureTitle = input<string | null>(null);
}
