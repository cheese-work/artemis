import { LoggerService } from '../../services/logger.service';
import { ChangeDetectionStrategy, Component, DestroyRef, Input, inject, signal } from '@angular/core';
import { ElementRef, ViewChild } from '@angular/core';
import { RunActionBarComponent } from '../run-presentation/run-action-bar.component';

@Component({
  selector: 'app-run-id-copy',
  standalone: true,
  imports: [RunActionBarComponent],
  template: `
    <span class="run-id-copy" appRunActionBar [compact]="true" [feedback]="feedback()"
      [actions]="[{ id: 'copy-id', label: 'ID: ' + runId.slice(0, 8), className: 'run-id-copy-button',
        ariaLabel: 'Copy full run ID ' + runId, title: 'Copy full run ID ' + runId }]"
      (action)="copyRunId($event.event)">
      @if (copyFailed()) {
        <span class="copy-fallback" (click)="$event.stopPropagation()">
          <input #fallbackInput type="text" [value]="runId" readonly aria-label="Full run ID to copy manually">
          <span>Press Ctrl+C to copy the full ID.</span>
        </span>
      }
    </span>
  `,
  styles: [`
    .run-id-copy { position: relative; display: inline-flex; }
    .copy-fallback { position: absolute; z-index: 3; top: calc(100% + 4px); left: 0; display: grid; gap: 5px; width: min(300px, 80vw); padding: 8px; border-radius: 8px; color: var(--color-surface); background: var(--color-ink); font-size: 11px; }
    .copy-fallback input { box-sizing: border-box; width: 100%; padding: 5px; border: 1px solid var(--color-text-faint); border-radius: 4px; color: var(--color-ink); font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; user-select: all; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunIdCopyComponent {
  private readonly logger = inject(LoggerService);
  private readonly destroyRef = inject(DestroyRef);
  private feedbackTimer: ReturnType<typeof setTimeout> | null = null;
  private selectionTimer: ReturnType<typeof setTimeout> | null = null;

  @ViewChild('fallbackInput') private fallbackInput?: ElementRef<HTMLInputElement>;

  @Input({ required: true }) public runId = '';
  public readonly feedback = signal('');

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
      if (this.selectionTimer) clearTimeout(this.selectionTimer);
    });
  }

  public readonly copyFailed = signal(false);

  public async copyRunId(event: Event): Promise<void> {
    event.stopPropagation();
    this.copyFailed.set(false);
    try {
      await navigator.clipboard.writeText(this.runId);
      this.feedback.set('Copied');
    } catch (error) {
      this.logger.warn('UI operation failed:', error);
      this.feedback.set('Copy failed');
      this.copyFailed.set(true);
      this.selectionTimer = setTimeout(() => {
        this.fallbackInput?.nativeElement.focus();
        this.fallbackInput?.nativeElement.select();
        this.fallbackInput?.nativeElement.setSelectionRange(0, this.runId.length);
      });
    }

    if (this.feedbackTimer) clearTimeout(this.feedbackTimer);
    this.feedbackTimer = setTimeout(() => this.feedback.set(''), 1600);
  }
}
