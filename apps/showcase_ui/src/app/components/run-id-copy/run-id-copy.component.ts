import { ChangeDetectionStrategy, Component, DestroyRef, Input, inject, signal } from '@angular/core';
import { ElementRef, ViewChild } from '@angular/core';

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
        (click)="copyRunId($event)"
      >
        ID: {{ runId.slice(0, 8) }}
      </button>
      @if (feedback()) {
        <span class="copy-feedback" role="status" aria-live="polite">{{ feedback() }}</span>
      }
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
    .run-id-copy-button { padding: 0; border: 0; color: inherit; background: none; font: inherit; text-align: left; cursor: pointer; }
    .run-id-copy-button:hover { color: #2563eb; text-decoration: underline; }
    .run-id-copy-button:focus-visible { outline: 2px solid #60a5fa; outline-offset: 3px; border-radius: 3px; }
    .copy-feedback { position: absolute; z-index: 2; left: 50%; bottom: calc(100% + 6px); padding: 5px 9px; transform: translateX(-50%); border-radius: 8px; color: white; background: #172033; font-size: 11px; white-space: nowrap; }
    .copy-fallback { position: absolute; z-index: 3; top: calc(100% + 4px); left: 0; display: grid; gap: 5px; width: min(300px, 80vw); padding: 8px; border-radius: 8px; color: #fff; background: #172033; font-size: 11px; }
    .copy-fallback input { box-sizing: border-box; width: 100%; padding: 5px; border: 1px solid #94a3b8; border-radius: 4px; color: #172033; font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; user-select: all; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunIdCopyComponent {
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
    } catch {
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
