import { ChangeDetectionStrategy, Component, DestroyRef, Input, inject, signal } from '@angular/core';

@Component({
  selector: 'app-run-id-copy',
  standalone: true,
  template: `
    <span class="run-id-copy">
      <button
        type="button"
        class="run-id-copy-button"
        [attr.aria-label]="'Copy full run ID ' + runId"
        [title]="'Copy full run ID ' + runId"
        (click)="copyRunId()"
      >
        ID: {{ runId.slice(0, 8) }}
      </button>
      @if (feedback()) {
        <span class="copy-feedback" role="status" aria-live="polite">{{ feedback() }}</span>
      }
    </span>
  `,
  styles: [`
    .run-id-copy { position: relative; display: inline-flex; }
    .run-id-copy-button { padding: 0; border: 0; color: inherit; background: none; font: inherit; text-align: left; cursor: pointer; }
    .run-id-copy-button:hover { color: #2563eb; text-decoration: underline; }
    .run-id-copy-button:focus-visible { outline: 2px solid #60a5fa; outline-offset: 3px; border-radius: 3px; }
    .copy-feedback { position: absolute; z-index: 2; left: 50%; bottom: calc(100% + 6px); padding: 5px 9px; transform: translateX(-50%); border-radius: 8px; color: white; background: #172033; font-size: 11px; white-space: nowrap; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunIdCopyComponent {
  private readonly destroyRef = inject(DestroyRef);
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;

  @Input({ required: true }) public runId = '';
  public readonly feedback = signal('');

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    });
  }

  public async copyRunId(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.runId);
      this.feedback.set('Copied');
    } catch {
      this.feedback.set('Copy failed');
    }

    if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    this.feedbackTimer = setTimeout(() => this.feedback.set(''), 1600);
  }
}
