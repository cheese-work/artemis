import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RunSummary } from '../../core/models/run.model';
import { GoalImage } from '../../core/models/session.model';
import { mediaUrl } from '../../utils/app-url.util';
import { LabelableDevice, deviceKindLabel, runDeviceLabel } from '../../utils/device-label.util';
import { mapRecording } from '../../utils/recording-state.util';
import { expiresText, interruptReason } from '../../utils/run-library-strings';
import { runTitle } from '../../utils/run-title.util';
import { runStatusView } from '../../utils/run-status.util';
import { OwnerLabelComponent } from '../owner-label/owner-label.component';

@Component({
  selector: '[appRunCard]',
  standalone: true,
  imports: [DatePipe, OwnerLabelComponent],
  template: `
    <span [class]="'run-icon tone-' + outcome().tone" aria-hidden="true">
      <span class="material-symbols-outlined">{{ outcome().icon }}</span>
    </span>
    <span class="run-content">
      <span class="run-prompt task-goal" [title]="prompt()">{{ prompt() }}</span>
      <span class="run-meta">
        <span class="run-outcome task-badge">{{ outcome().label }}</span>
        @if (run().app_package) { <span class="run-package mono"> · {{ run().app_package }}</span> }
      </span>
    </span>
    <span class="run-date mono" [title]="run().start_time === null ? 'Date unknown' : (run().start_time! * 1000 | date: 'MMM d, y, h:mm a')!">
      @if (run().start_time !== null) { {{ run().start_time! * 1000 | date: 'HH:mm' }} }
      @else { — }
    </span>
    <span class="run-facts sr-only">
      @if (recording(); as badge) { <span class="run-recording">{{ badge }}</span> }
      <span class="run-device task-device" [title]="run().device_ref?.serial ?? 'Unknown phone'">
        <span class="device-name" [class.run-serial]="deviceLabel() === run().device_ref?.serial" [class.mono]="deviceLabel() === run().device_ref?.serial">{{ deviceLabel() }}</span>
        @if (queue() && device(); as phone) { <span> · {{ deviceKindLabel(phone) }}</span> }
        @if (run().host_id !== null || computer() !== 'A browser') { <span> · {{ computer() }}</span> }
      </span>
      @if (current()) { <span class="run-current">Viewing</span> }
      @if (showOwner()) {
        @if (queue()) { <app-owner-label [owner]="run().requested_by"></app-owner-label> }
        @else { <span class="run-owner">Owner: {{ run().requested_by ?? 'No owner' }}</span> }
      }
      @if (expires(); as text) { <span class="run-expires mono">{{ text }}</span> }
      @if (run().status === 'interrupted') { <span class="run-reason">{{ interruptReason(run().interrupt_reason) }}</span> }
      @if (goalImages().length) {
        <span class="task-goal-images">
          @for (image of goalImages(); track image.index) {
            <img [src]="mediaUrl(image.url)" [alt]="'Image ' + (image.index + 1) + ' sent with this task'" loading="lazy">
          }
        </span>
      }
    </span>
  `,
  styles: [`
    :host { position: relative; display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: center; gap: 8px; height: 56px; min-width: 44px; padding: 4px 12px; box-sizing: border-box; color: var(--color-ink); text-decoration: none; }
    .run-icon { display: flex; align-items: center; justify-content: center; }
    .run-icon .material-symbols-outlined { font-size: 20px; }
    .tone-ok { color: var(--status-ok-fg); }
    .tone-warn { color: var(--status-warn-fg); }
    .tone-danger { color: var(--status-danger-fg); }
    .tone-neutral { color: var(--color-text-muted); }
    .run-content { min-width: 0; }
    .run-prompt { display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; font-size: 14px; line-height: 16px; font-weight: 500; }
    .run-meta { display: flex; overflow: hidden; white-space: nowrap; color: var(--color-text-muted); font-size: 12px; line-height: 16px; }
    .run-package { overflow: hidden; text-overflow: ellipsis; }
    .run-date { align-self: start; margin-top: 4px; color: var(--color-text-faint); font-size: 12px; line-height: 16px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunCardComponent {
  readonly run = input.required<RunSummary>();
  readonly computer = input('A browser');
  readonly device = input<LabelableDevice | null>(null);
  readonly showOwner = input(false);
  readonly queue = input(false);
  readonly current = input(false);
  readonly goalImages = input<GoalImage[]>([]);
  readonly mediaUrl = mediaUrl;
  readonly interruptReason = interruptReason;
  readonly deviceKindLabel = deviceKindLabel;
  readonly prompt = computed(() => runTitle(this.run().prompt));
  readonly outcome = computed(() => {
    const status = runStatusView(this.run().status);
    switch (this.run().verdict) {
      case 'pass': return { label: 'Pass', icon: 'check_circle', tone: 'ok' };
      case 'fail': return { label: 'Fail', icon: 'cancel', tone: 'danger' };
      case 'inconclusive': return { label: 'Inconclusive', icon: 'help', tone: 'warn' };
      default: return { ...status, label: status.key === 'completed' ? 'Completed' : status.label };
    }
  });
  readonly deviceLabel = computed(() => runDeviceLabel(this.run().device_ref?.serial ?? 'Unknown phone', this.device()));
  readonly expires = computed(() => expiresText(this.run().expires_at, Math.floor(Date.now() / 1000)));
  readonly recording = computed(() => {
    const recording = this.run().recordings[0];
    const view = mapRecording({ capture: (recording?.capture ?? null) as never, transfer: (recording?.transfer ?? null) as never, playback: null });
    return view.state === 'unknown' ? null : view.badge;
  });
}
