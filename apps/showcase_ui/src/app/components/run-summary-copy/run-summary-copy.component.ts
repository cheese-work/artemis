import { LoggerService } from '../../services/logger.service';
import { inject, ChangeDetectionStrategy, Component, Input, signal } from '@angular/core';
import { Session } from '../../core/models/session.model';
import { buildRunSummary } from '../../utils/run-copy.util';
import { RunActionBarComponent } from '../run-presentation/run-action-bar.component';

@Component({
  selector: 'app-run-summary-copy',
  standalone: true,
  imports: [RunActionBarComponent],
  template: `
    <span class="run-summary-copy" appRunActionBar [compact]="true" [feedback]="feedback()"
      [actions]="[{ id: 'copy-summary', label: 'Copy run summary', icon: 'content_copy',
        className: 'copy-summary-button', ariaLabel: 'Copy run summary' }]" (action)="copySummary()"></span>
  `,
  styles: [`
    .run-summary-copy { position: relative; display: inline-flex; }
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
