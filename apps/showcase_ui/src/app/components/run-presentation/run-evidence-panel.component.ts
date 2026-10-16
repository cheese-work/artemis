import { ChangeDetectionStrategy, Component, ElementRef, computed, input, output, signal, viewChild } from '@angular/core';
import { RecordingView } from '../../utils/recording-state.util';

export interface RunEvidenceStep {
  id: string;
  number: number;
  title: string;
  screenshot: string | null;
}

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
      @if (recording()?.state === 'in_progress' || recording()?.state === 'preparing') {
        <span class="recording-chip">
          @if (recording()?.state === 'in_progress') { <span class="recording-dot" aria-hidden="true"></span> }
          {{ recording()?.badge }}
        </span>
      }
      @if (canPlay() && hasScreenshots()) {
        <div class="media-switch" role="group" aria-label="Evidence view">
          <button type="button" data-media="screenshot" [attr.aria-pressed]="!showVideo()" (click)="media.set('screenshot')">Screenshot</button>
          <button type="button" data-media="video" [attr.aria-pressed]="showVideo()" (click)="media.set('video'); selectIndex(selectedIndex())">Video</button>
        </div>
      }
      <div class="phone-frame" [class.video-active]="showVideo()">
        @if (showVideo()) {
          <video #player class="evidence-video" controls controlsList="nodownload" playsinline [src]="videoUrl()"
            (loadedmetadata)="metadata.emit()" (ended)="ended.emit()" (error)="failed.emit()"></video>
        } @else if (screenshotUrl(); as src) {
          <img class="evidence-image" [src]="src" [alt]="screenshotAlt()" />
        } @else {
          <p class="frame-placeholder">No screenshot</p>
        }
      </div>
      @if (steps().length) {
        <div class="screenshot-filmstrip" role="group" aria-label="Step screenshots">
          @for (step of steps(); track step.id) {
            <button type="button" class="filmstrip-step" [attr.aria-current]="selectedIndex() === $index ? 'step' : null"
              [attr.aria-label]="'Step ' + step.number + ': ' + step.title" (click)="selectIndex($index)">
              @if (step.screenshot; as src) { <img [src]="src" alt="" /> }
              @else { <span class="no-image">No image</span> }
              <span class="filmstrip-number">{{ step.number }}</span>
            </button>
          }
        </div>
        <input #scrubber class="step-scrubber" type="range" min="0" [max]="steps().length - 1" step="1"
          [value]="selectedIndex()" [disabled]="steps().length < 2" aria-label="Select step"
          [attr.aria-valuetext]="'Step ' + selectedStep()?.number + ' of ' + steps().length + ': ' + selectedStep()?.title"
          (input)="selectIndex(scrubber.valueAsNumber)" />
      }
      <div class="evidence-caption">
        @if (selectedStep(); as step) { <p class="step-caption">Step {{ step.number }} · {{ step.title }}</p> }
        @if (recording()?.ribbon; as ribbon) { <p class="recording-ribbon">{{ ribbon }}</p> }
        @if (!showVideo()) { <p class="recording-copy" [attr.role]="announce() ? 'status' : null">{{ message() ?? recording()?.copy }}</p> }
        @if (detail(); as technical) {
          <details class="technical-details">
            <summary>Technical details</summary>
            <pre class="technical-body">{{ technical }}</pre>
          </details>
        }
        @if (retryable()) { <button type="button" class="secondary-button" (click)="retry.emit()">Check again</button> }
        @if (!showVideo() && !screenshotUrl() && loaded()) { <p class="muted">No screenshots for this run.</p> }
      </div>
    }
  `,
  styleUrl: './run-evidence-panel.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunEvidencePanelComponent {
  readonly presentation = input<'panel' | 'trigger'>('panel');
  readonly recording = input<RecordingView | null>(null);
  readonly announce = input(true);
  readonly videoUrl = input<string | null>(null);
  readonly screenshotUrl = input<string | null>(null);
  readonly screenshotAlt = input('');
  readonly loaded = input(false);
  readonly playerFailed = input(false);
  readonly message = input<string | null>(null);
  readonly detail = input<string | null>(null);
  readonly retryable = input(false);
  readonly running = input(false);
  readonly playbackStatus = input<string | null | undefined>(null);
  readonly hasSteps = input(false);
  readonly screenshotCount = input(0);
  readonly steps = input<readonly RunEvidenceStep[]>([]);
  readonly selectedStepId = input<string | null>(null);
  readonly stepSelected = output<string>();
  readonly metadata = output<void>();
  readonly ended = output<void>();
  readonly failed = output<void>();
  readonly retry = output<void>();
  readonly player = viewChild<ElementRef<HTMLVideoElement>>('player');
  readonly media = signal<'screenshot' | 'video' | null>(null);
  readonly canPlay = computed(() => !!this.recording()?.playable && !!this.videoUrl() && !this.playerFailed());
  readonly hasScreenshots = computed(() => !!this.screenshotUrl() || this.steps().some(step => !!step.screenshot));
  readonly showVideo = computed(() => this.canPlay() && (this.media() === 'video' || (this.media() === null && !this.running())));
  readonly selectedIndex = computed(() => {
    const index = this.steps().findIndex(step => step.id === this.selectedStepId());
    return index < 0 ? Math.max(0, this.steps().length - 1) : index;
  });
  readonly selectedStep = computed(() => this.steps()[this.selectedIndex()] ?? null);

  selectIndex(index: number): void {
    if (Number.isInteger(index) && index >= 0 && index < this.steps().length) this.stepSelected.emit(this.steps()[index].id);
  }

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
