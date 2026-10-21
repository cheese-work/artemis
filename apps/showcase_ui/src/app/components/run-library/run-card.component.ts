import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { ChatTurnResult, RunSummary } from '../../core/models/run.model';
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
    <span class="run-content" data-testid="run-card-identity">
      @if (resultState() === 'loading') {
        <span class="run-result-state" data-testid="run-card-result-state" data-label-state="loading">Đang tải kết quả</span>
      } @else if (resultState() === 'pending') {
        <span class="run-result-state" data-testid="run-card-result-state" data-label-state="pending">Đã kết thúc · Đang xác định kết quả</span>
      } @else if (resultState() === 'cancelled') {
        <span class="run-result-state" data-testid="run-card-result-state" data-label-state="cancelled">Đã huỷ</span>
      } @else if (resultState() === 'legacy') {
        <span class="run-result-state" data-testid="run-card-result-state" data-label-state="legacy">Run cũ — chưa có verdict</span>
      } @else if (resultState() === 'error') {
        <span class="run-result-state" data-testid="run-card-result-state" data-label-state="error">Không tải được kết quả · Thử lại</span>
      } @else if (result(); as verdict) {
        <span class="run-card-verdict" data-testid="run-card-verdict" [attr.data-catalog-key]="verdict.catalog_key" data-label-state="final">
          <span class="run-verdict-heading"><strong>{{ verdictLabel(verdict) }}</strong><span>Verification · {{ verdict.verification }}</span></span>
          <code>{{ verdict.catalog_key }}</code>
          @if (verdict.reason) { <span class="run-verdict-reason">{{ verdict.reason }}</span> }
          <span class="run-verdict-evidence" data-testid="run-card-evidence">{{ evidenceLabel(verdict) }}</span>
          @if (verdict.action.text) { <span class="run-verdict-action">→ {{ verdict.action.text }}</span> }
          @if (verdict.responsible) { <span class="run-verdict-responsible">Responsible · {{ verdict.responsible }}</span> }
          <span class="run-verdict-turn">Kết quả của lượt {{ verdict.turn_index }}/{{ verdict.turn_count }}</span>
        </span>
      }
      <span class="run-prompt task-goal" [title]="prompt()">{{ prompt() }}</span>
      <span class="run-meta">
        <span class="run-outcome task-badge">{{ outcome().label }}</span>
        @if (run().app_package) { <span class="run-package mono"> · {{ run().app_package }}</span> }
        @if (run().device_ref?.serial; as serial) { <span class="run-serial mono"> · {{ serial }}</span> }
      </span>
    </span>
    <span class="run-date mono" [title]="run().start_time === null ? 'Date unknown' : (run().start_time! * 1000 | date: 'MMM d, y, HH:mm' : '+0700') + ' ICT'">
      @if (run().start_time !== null) { {{ run().start_time! * 1000 | date: 'HH:mm' : '+0700' }} ICT }
      @else { — }
    </span>
    <span class="run-facts sr-only">
      @if (recording(); as badge) { <span class="run-recording">{{ badge }}</span> }
      <span class="run-device task-device" [title]="run().device_ref?.serial ?? 'Unknown phone'">
        <span class="device-name">{{ deviceLabel() }}</span>
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
    :host { position: relative; display: grid; grid-template-columns: 24px minmax(0, 1fr) auto; align-items: start; gap: var(--space-sm); min-height: 56px; min-width: 44px; padding: var(--space-smd); box-sizing: border-box; color: var(--color-ink); text-decoration: none; }
    .run-icon { display: flex; align-items: center; justify-content: center; }
    .run-icon .material-symbols-outlined { font-size: 20px; }
    .tone-ok { color: var(--status-ok-fg); }
    .tone-warn { color: var(--status-warn-fg); }
    .tone-danger { color: var(--status-danger-fg); }
    .tone-neutral { color: var(--color-text-muted); }
    .run-content { display: grid; min-width: 0; gap: var(--space-xs); }
    .run-card-verdict { display: grid; gap: var(--space-xs); min-width: 0; white-space: normal; }
    .run-verdict-heading { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-sm); }
    .run-verdict-heading strong { color: var(--color-ink); }
    .run-verdict-heading span, .run-verdict-action, .run-verdict-responsible, .run-verdict-turn { color: var(--color-text-muted); font-size: 12px; line-height: 16px; }
    .run-card-verdict code { color: var(--color-text-muted); font-size: 11px; overflow-wrap: anywhere; }
    .run-verdict-reason, .run-verdict-evidence { color: var(--color-ink); font-size: 12px; line-height: 16px; overflow-wrap: anywhere; }
    .run-result-state { color: var(--color-text-muted); font-size: 12px; line-height: 16px; }
    .run-prompt { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 14px; line-height: 16px; font-weight: 500; }
    .run-meta { display: flex; gap: var(--space-xs); overflow: hidden; white-space: nowrap; color: var(--color-text-muted); font-size: 12px; line-height: 16px; }
    .run-package, .run-serial { overflow: hidden; text-overflow: ellipsis; }
    .run-date { align-self: start; color: var(--color-text-faint); font-size: 12px; line-height: 16px; }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunCardComponent {
  readonly run = input.required<RunSummary>();
  readonly result = input<ChatTurnResult | null>(null);
  readonly resultState = input<'none' | 'loading' | ChatTurnResult['label_state']>('none');
  readonly computer = input('A browser');
  readonly device = input<LabelableDevice | null>(null);
  readonly showOwner = input(false);
  readonly queue = input(false);
  readonly current = input(false);
  readonly goalImages = input<GoalImage[]>([]);
  readonly mediaUrl = mediaUrl;
  readonly interruptReason = interruptReason;
  readonly deviceKindLabel = deviceKindLabel;
  readonly prompt = computed(() => this.run().chat_title || runTitle(this.run().chat_prompt ?? this.run().prompt));
  readonly outcome = computed(() => {
    const status = runStatusView(this.run().status);
    switch (this.run().verdict) {
      case 'pass': return { label: 'Pass', icon: 'check_circle', tone: 'ok' };
      case 'fail': return { label: 'Fail', icon: 'cancel', tone: 'danger' };
      case 'inconclusive': return { label: 'Inconclusive', icon: 'help', tone: 'warn' };
      default: return status.key === 'completed' ? { label: 'Completed', icon: 'description', tone: 'neutral' } : status;
    }
  });
  readonly verdictLabel = (result: ChatTurnResult): string => result.verdict === 'pass' ? 'Pass'
    : result.verdict === 'fail' ? 'Fail' : result.verdict === 'blocked' ? 'Blocked' : result.verdict;
  readonly evidenceLabel = (result: ChatTurnResult): string => `Decisive evidence · Turn ${result.turn_index}`
    + (result.evidence.step !== null ? ` · Step ${result.evidence.step}` : result.evidence.screenshot_url ? ' · Screenshot' : '');
  readonly deviceLabel = computed(() => runDeviceLabel(this.run().device_ref?.serial ?? 'Unknown phone', this.device()));
  readonly expires = computed(() => expiresText(this.run().expires_at, Math.floor(Date.now() / 1000)));
  readonly recording = computed(() => {
    const recording = this.run().recordings[0];
    const view = mapRecording({ capture: (recording?.capture ?? null) as never, transfer: (recording?.transfer ?? null) as never, playback: null });
    return view.state === 'unknown' ? null : view.badge;
  });
}
