import { LoggerService } from '../../services/logger.service';
import { inject, ChangeDetectionStrategy, Component, Input, signal } from '@angular/core';
import { Session } from '../../core/models/session.model';
import { buildRunSummary } from '../../utils/run-copy.util';

@Component({
  selector: 'app-run-summary-copy',
  standalone: true,
  template: `
    <span class="run-summary-copy">
      <button type="button" class="copy-summary-button" aria-label="Copy run summary" (click)="copySummary()">
        <span class="material-symbols-outlined" aria-hidden="true">content_copy</span>
        <span>Copy run summary</span>
      </button>
      @if (feedback()) {
        <span class="copy-feedback" role="status" aria-live="polite">{{ feedback() }}</span>
      }
    </span>
  `,
  styles: [`
    .run-summary-copy { position: relative; display: inline-flex; }
    .copy-summary-button { display: inline-flex; align-items: center; gap: 5px; height: 30px; padding: 0 11px; border: 1px solid #e2e8f0; border-radius: 15px; color: #334155; background: #ffffffe0; font: inherit; font-size: 12px; font-weight: 600; cursor: pointer; }
    .copy-summary-button:hover { background: #fff; color: #1d4ed8; }
    .copy-summary-button:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
    .copy-summary-button .material-symbols-outlined { font-size: 15px; }
    .copy-feedback { position: absolute; z-index: 2; left: 50%; bottom: calc(100% + 6px); padding: 5px 9px; transform: translateX(-50%); border-radius: 8px; color: white; background: #172033; font-size: 11px; white-space: nowrap; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunSummaryCopyComponent {
  private readonly logger = inject(LoggerService);
  @Input({ required: true }) public session!: Session;
  @Input({ required: true }) public currentStatus = 'unknown';
  @Input() public logs: unknown[] = [];
  @Input() public recordingUrl: string | null | undefined;
  public readonly feedback = signal('');
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;

  public async copySummary(): Promise<void> {
    const summary = buildRunSummary(this.session, this.currentStatus, this.logs, this.recordingUrl);
    try {
      await navigator.clipboard.writeText(summary);
      this.feedback.set('Copied');
    } catch (error) {
      this.logger.warn('UI operation failed:', error);
      this.feedback.set('Copy failed');
    }

    if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    this.feedbackTimer = setTimeout(() => this.feedback.set(''), 1600);
  }
}
