import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RunSummary } from '../../core/models/run.model';
import { GoalImage } from '../../core/models/session.model';
import { mediaUrl } from '../../utils/app-url.util';
import { LabelableDevice, runDeviceLabel } from '../../utils/device-label.util';
import { mapRecording } from '../../utils/recording-state.util';
import { expiresText, interruptReason, truncate } from '../../utils/run-library-strings';
import { RunStatusBadgeComponent } from '../run-presentation/run-status-badge.component';

@Component({
  selector: '[appRunCard]',
  standalone: true,
  imports: [DatePipe, RunStatusBadgeComponent],
  template: `
    <span class="run-prompt" [title]="run().prompt ?? ''">{{ prompt() }}</span>
    @if (goalImages().length) {
      <span class="task-goal-images">
        @for (image of goalImages(); track image.index) {
          <img [src]="mediaUrl(image.url)" [alt]="'Image ' + (image.index + 1) + ' sent with this task'" loading="lazy">
        }
      </span>
    }
    <span class="run-outcome" appRunStatusBadge [status]="run().status"></span>
    <span class="run-meta">
      <span class="run-recording">{{ recording() }}</span>
      <span class="run-device" [title]="run().device_ref?.serial ?? 'Unknown phone'">
        {{ deviceLabel() }} · {{ computer() }}
      </span>
      <span class="run-date">
        @if (run().start_time) { {{ run().start_time! * 1000 | date: 'MMM d, y, h:mm a' }} }
        @else { Date unknown }
      </span>
      @if (current()) { <span class="run-current">Viewing</span> }
      @if (showOwner()) {
        <span class="run-owner">Owner: {{ run().requested_by ?? 'No owner' }}</span>
        <span class="run-read-only">Read-only</span>
      }
      @if (expires(); as text) { <span class="run-expires">{{ text }}</span> }
      @if (run().status === 'interrupted') { <span class="run-reason">{{ interruptReason(run().interrupt_reason) }}</span> }
    </span>
  `,
  styles: [`
    .run-prompt { overflow: hidden; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
    .run-outcome { display: inline-flex; justify-self: start; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
    .tone-ok { color: var(--status-ok-fg); background: var(--status-ok-bg); }
    .tone-warn { color: var(--status-warn-fg); background: var(--status-warn-bg); }
    .tone-danger { color: var(--status-danger-fg); background: var(--status-danger-bg); }
    .tone-neutral { color: var(--status-neutral-fg); background: var(--status-neutral-bg); }
    .run-meta { display: flex; flex-wrap: wrap; grid-column: 1 / -1; gap: 4px 16px; font-size: 12px; color: var(--evidence-muted); }
    .run-device { display: inline-flex; align-items: center; gap: 4px; }
    .run-current { font-weight: 600; color: var(--color-ink); }
    .run-expires { font-weight: 600; color: var(--status-warn-fg); }
    .run-owner { overflow-wrap: anywhere; }
    .task-goal-images { display: flex; grid-column: 1 / -1; flex-wrap: wrap; gap: 8px; }
    .task-goal-images img { width: 64px; height: 64px; object-fit: cover; border-radius: 8px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunCardComponent {
  readonly run = input.required<RunSummary>();
  readonly computer = input('A browser');
  readonly device = input<LabelableDevice | null>(null);
  readonly showOwner = input(false);
  /** This run is the one open beside the list. */
  readonly current = input(false);
  readonly goalImages = input<GoalImage[]>([]);
  readonly mediaUrl = mediaUrl;
  readonly interruptReason = interruptReason;
  readonly prompt = computed(() => truncate(this.run().prompt));
  readonly deviceLabel = computed(() => runDeviceLabel(this.run().device_ref?.serial ?? 'Unknown phone', this.device()));
  readonly expires = computed(() => expiresText(this.run().expires_at, Math.floor(Date.now() / 1000)));
  readonly recording = computed(() => {
    const recording = this.run().recordings[0];
    return mapRecording({ capture: (recording?.capture ?? null) as never, transfer: (recording?.transfer ?? null) as never, playback: null }).badge;
  });
}
