import { ChangeDetectionStrategy, Component, ElementRef, computed, input, output, viewChild } from '@angular/core';
import { RecordingView } from '../../utils/recording-state.util';

@Component({
  selector: '[appRunEvidencePanel]',
  standalone: true,
  template: `
    @if (presentation() === 'trigger') {
      <span class="material-symbols-outlined video-icon" [class.spin-animation]="processing()"
        [class.play-icon]="!running() && !processing() && !!videoUrl()"
        [class.steps-icon]="!running() && !processing() && !videoUrl() && hasSteps()"
        [class.error-icon]="!running() && !processing() && !videoUrl() && !hasSteps() && playbackStatus() === 'failed'">{{ trigger().icon }}</span>
      <span class="btn-text">{{ trigger().label }}</span>
      @if (running()) { <span class="video-pulse-dot red"></span> }
      @if (!running() && !videoUrl() && hasSteps()) { <span class="steps-count-badge">{{ screenshotCount() }}</span> }
    } @else {
      @if (recording()?.playable && videoUrl() && !playerFailed()) {
        @if (recording()?.ribbon; as ribbon) { <p class="recording-ribbon">{{ ribbon }}</p> }
        <video #player class="evidence-video" controls controlsList="nodownload" playsinline [src]="videoUrl()"
          (loadedmetadata)="metadata.emit()" (ended)="ended.emit()" (error)="failed.emit()"></video>
      } @else {
        <p class="recording-copy" role="status">{{ message() ?? recording()?.copy }}</p>
        @if (retryable()) { <button type="button" class="secondary-button" (click)="retry.emit()">Check again</button> }
        @if (screenshotUrl(); as src) { <img class="evidence-image" [src]="src" [alt]="screenshotAlt()" /> }
        @else if (loaded()) { <p class="muted">No screenshots for this run.</p> }
      }
    }
  `,
  styleUrl: './run-evidence-panel.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunEvidencePanelComponent {
  readonly presentation = input<'panel' | 'trigger'>('panel');
  readonly recording = input<RecordingView | null>(null);
  readonly videoUrl = input<string | null>(null);
  readonly screenshotUrl = input<string | null>(null);
  readonly screenshotAlt = input('');
  readonly loaded = input(false);
  readonly playerFailed = input(false);
  readonly message = input<string | null>(null);
  readonly retryable = input(false);
  readonly running = input(false);
  readonly playbackStatus = input<string | null | undefined>(null);
  readonly hasSteps = input(false);
  readonly screenshotCount = input(0);
  readonly metadata = output<void>();
  readonly ended = output<void>();
  readonly failed = output<void>();
  readonly retry = output<void>();
  readonly player = viewChild<ElementRef<HTMLVideoElement>>('player');
  readonly processing = computed(() => !this.running() && this.playbackStatus() === 'processing');
  readonly trigger = computed(() => {
    if (this.running()) return { icon: 'videocam', label: 'Recording...' };
    if (this.processing()) return { icon: 'progress_activity', label: 'Preparing...' };
    if (this.videoUrl()) return { icon: 'smart_display', label: 'Play Recording' };
    if (this.hasSteps()) return { icon: 'slideshow', label: 'Step Replay' };
    if (this.playbackStatus() === 'failed') return { icon: 'videocam_off', label: 'Recording Failed' };
    return { icon: 'videocam', label: 'Screen Recording' };
  });
}
