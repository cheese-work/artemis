import { ChangeDetectionStrategy, Component, computed, inject, input, output } from '@angular/core';
import { AgentService } from '../../services/agent.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { interruptedSentence, interruptReason, RUN_STRINGS } from '../../utils/run-library-strings';

/**
 * Shown over the dock while the run on screen was interrupted. Reconnecting only brings the
 * phone back; the run is over, so the next step after that is a new run with the same prompt.
 */
@Component({
  selector: 'app-interrupted-banner',
  standalone: true,
  template: `
    @if (phone.runInterrupted() && reconnectOnly()) {
      @if (!phone.target()) {
        <button type="button" class="action" [disabled]="!phone.canConnectFromBrowser()" (click)="reconnect()">Reconnect phone</button>
      }
    } @else if (phone.runInterrupted()) {
      <div class="banner" role="status" aria-live="polite">
        <p class="headline">{{ sentence() }}</p>
        <p class="reason">{{ reason() }}</p>
        @if (phone.target()) {
          <button type="button" class="action" (click)="startNewRun.emit(prompt())">{{ strings.startNewRun }}</button>
        } @else {
          <button type="button" class="action" [disabled]="!phone.canConnectFromBrowser()" (click)="reconnect()">Reconnect phone</button>
        }
      </div>
    }
  `,
  styles: [`
    :host { display: block; pointer-events: auto; }
    .banner {
      display: flex; flex-wrap: wrap; align-items: center; gap: .25rem .75rem; padding: .75rem 1rem;
      border: 1px solid var(--color-warning); border-radius: 14px; background: var(--color-warning-bg); color: var(--color-warning); font-size: .9rem;
      box-shadow: 0 8px 24px -8px rgb(15 23 42 / 20%);
    }
    p { margin: 0; }
    .headline { font-weight: 600; flex: 1 1 100%; }
    .reason { flex: 1 1 auto; }
    .action {
      min-height: 44px; min-width: 44px; padding: 0 1rem; border: 1px solid var(--color-warning); border-radius: 10px;
      background: var(--color-surface); color: var(--color-warning); font: inherit; font-weight: 600; cursor: pointer;
    }
    .action:disabled { opacity: .55; cursor: not-allowed; }
    .action:focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class InterruptedBannerComponent {
  protected readonly phone = inject(WorkspacePhoneService);
  private readonly agent = inject(AgentService);
  protected readonly strings = RUN_STRINGS;

  /** The interrupted run's prompt, handed back so a new run can start from it. */
  public readonly startNewRun = output<string>();
  public readonly reconnectOnly = input(false);

  protected readonly prompt = computed(() => this.agent.currentSession()?.initial_goal ?? '');
  /** The last step the run reached, from the run on screen; null when no step ran. */
  public readonly lastStep = input<number | null>(null);
  protected readonly sentence = computed(() => interruptedSentence(this.lastStep()));
  protected readonly reason = computed(() => interruptReason(this.agent.currentSession()?.interrupt_reason));

  protected reconnect(): void {
    void this.phone.connectFromBrowser();
  }
}
