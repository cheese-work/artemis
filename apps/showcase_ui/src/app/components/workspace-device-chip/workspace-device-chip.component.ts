import { ChangeDetectionStrategy, Component, DestroyRef, ElementRef, Injector, afterNextRender, computed, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { deviceKindLabel, deviceTitle } from '../../utils/device-label.util';

let nextId = 0;

/**
 * The Workspace's only phone control. It shows the phone the next run uses and opens a picker
 * to connect from this browser, switch to another phone, or disconnect. Everything here works
 * for any signed-in person; none of it touches the shared adb server.
 */
@Component({
  selector: 'app-workspace-device-chip',
  standalone: true,
  imports: [RouterLink],
  template: `
    <div class="chip-host" (keydown.escape)="close(true)" (focusout)="onFocusOut($event)">
      <div class="phone-card-header">
        <span class="material-symbols-outlined" aria-hidden="true">smartphone</span>
        <div>
          <span class="phone-card-label">{{ deviceLabel() }}</span>
          @if (phone.target(); as target) { <span class="phone-card-serial">{{ target.serial }}</span> }
        </div>
      </div>
      <p class="phone-card-state">
        <span class="material-symbols-outlined" aria-hidden="true">{{ phone.view().icon }}</span>
        {{ stateLabel() }}@if (queuedCount()) { · {{ queuedCount() }} queued }
      </p>
      <button
        #chipButton
        type="button"
        class="chip"
        [class]="phone.view().kind"
        aria-haspopup="true"
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="panelId"
        [attr.aria-describedby]="statusId"
        [title]="phone.view().text"
        (click)="toggle()"
      >
        <span class="material-symbols-outlined icon" [class.spinning]="phone.view().kind === 'connecting'" aria-hidden="true">{{ phone.view().icon }}</span>
        <span class="text">{{ phone.view().text }}</span>
        <span class="phone-card-action">{{ phone.target() ? 'Phones' : 'Connect a phone' }}</span>
      </button>
      <span [id]="statusId" class="visually-hidden" role="status" aria-live="polite">{{ phone.view().text }}. {{ phone.view().hint }}</span>

      @if (open()) {
        <div #panel class="panel" role="group" aria-label="Phones" [id]="panelId">
          <p class="hint">{{ phone.view().hint }}</p>
          @if (phone.connectionError(); as error) {
            <p class="error" role="alert">{{ error }}</p>
          }
          @if (phone.options().length > 0) {
            <ul class="options">
              @for (option of phone.options(); track option.serial) {
                <li>
                  <button
                    type="button"
                    class="option"
                    [attr.aria-pressed]="option.serial === phone.view().target?.serial"
                    [attr.aria-describedby]="option.note ? noteId(option.serial) : null"
                    [disabled]="phone.runActive() || !option.usable"
                    (click)="choose(option.serial)"
                  >
                    <span class="option-text">{{ option.text }}</span>
                    @if (option.serial === phone.view().target?.serial) {
                      <span class="material-symbols-outlined" aria-hidden="true">check</span>
                    }
                  </button>
                  <span class="device-detail">Connection: {{ option.serial }}</span>
                  @if (option.note) {
                    <span class="note" [id]="noteId(option.serial)">{{ option.note }}</span>
                  }
                </li>
              }
            </ul>
          } @else {
            <p class="empty">No phones are listed yet.</p>
          }
          @if (phone.runActive()) {
            <p class="note">Phone in use by this run.</p>
          }
          @if (phone.view().kind === 'other-tab') {
            <button type="button" class="action" [disabled]="phone.runActive()" (click)="useHere()">
              <span class="material-symbols-outlined" aria-hidden="true">tab</span>
              Use here
            </button>
          } @else if (phone.view().kind === 'dropped' || phone.view().kind === 'interrupted') {
            <button type="button" class="action" [disabled]="!phone.canConnectFromBrowser()" (click)="connect()">
              <span class="material-symbols-outlined" aria-hidden="true">refresh</span>
              Reconnect
            </button>
          } @else {
            <button
              type="button"
              class="action"
              [disabled]="!phone.canConnectFromBrowser()"
              (click)="connect()"
            >
              <span class="material-symbols-outlined" aria-hidden="true">usb</span>
              Connect a phone from this browser
            </button>
          }
          @if (phone.browserPhoneConnected()) {
            @if (confirmingDisconnect()) {
              <p class="note" role="alert">Disconnecting stops the run on this phone.</p>
              <div class="confirm">
                <button type="button" class="action danger" (click)="disconnect()">Disconnect and stop</button>
                <button type="button" class="action" (click)="confirmingDisconnect.set(false)">Keep phone</button>
              </div>
            } @else {
              <button type="button" class="action" (click)="askDisconnect()">Disconnect</button>
            }
          }
          <a class="action more" routerLink="/setup" (click)="close(false)">More options</a>
        </div>
      }
    </div>
  `,
  styles: [`
    :host { display: inline-flex; pointer-events: auto; }
    .chip-host { position: relative; }
    .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    .chip, .option, .action {
      min-height: 44px; min-width: 44px; box-sizing: border-box; font: inherit; cursor: pointer;
    }
    .chip {
      display: inline-flex; align-items: center; gap: .45rem; max-width: 22rem; padding: 0 .9rem;
      border: 0; border-radius: var(--radius-full); background: var(--color-surface-subtle); color: var(--color-ink);
      font-size: .85rem; font-weight: 600;
    }
    .chip .text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chip.connected .icon { color: var(--color-success); }
    .chip.dropped, .chip.interrupted, .chip.other-tab { background: var(--color-warning-bg); }
    .chip.dropped .icon, .chip.interrupted .icon, .chip.other-tab .icon { color: var(--color-warning); }
    .chip.none .icon { color: var(--color-text-faint); }
    .spinning { animation: chip-spin 1s linear infinite; }
    @keyframes chip-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .spinning { animation: none; } }
    .panel {
      position: absolute; top: calc(100% + 8px); left: 0; z-index: 60; width: 21rem; max-width: calc(100vw - 2rem);
      display: flex; flex-direction: column; gap: .5rem; padding: .75rem; border: 1px solid var(--color-rule); border-radius: 14px;
      background: var(--color-surface); color: var(--color-ink); box-shadow: 0 12px 32px -8px rgb(15 23 42 / 25%);
    }
    .hint, .empty, .note, .device-detail { margin: 0; font-size: .85rem; color: var(--color-text-muted); }
    .device-detail { display: block; padding: .25rem .75rem; overflow-wrap: anywhere; }
    .error { margin: 0; font-size: .85rem; color: var(--color-error); white-space: pre-line; }
    .options { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: .25rem; }
    .option, .action {
      display: flex; align-items: center; justify-content: space-between; gap: .5rem; width: 100%; padding: 0 .75rem;
      border: 0; border-radius: var(--radius-md); background: var(--color-surface-subtle); color: inherit; font-size: .9rem; text-align: left;
    }
    .option[aria-pressed='true'] { color: var(--color-success); background: var(--color-success-bg); font-weight: 600; }
    a.action { text-decoration: none; }
    .option:disabled, .action:disabled { opacity: .55; cursor: not-allowed; }
    .action { justify-content: center; }
    .action.danger { background: var(--color-error-bg); color: var(--color-error); }
    .confirm { display: flex; gap: .5rem; }
    button:focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }
    .phone-card-header, .phone-card-state, .phone-card-action { display: none; }
    @media (min-width: 1024px) {
      :host { display: block; width: 100%; }
      .chip-host { background: var(--color-surface); border-radius: var(--radius-lg); }
      .phone-card-header { display: flex; align-items: center; gap: 8px; padding: 12px; border-radius: var(--radius-lg) var(--radius-lg) 0 0; background: var(--color-device); color: var(--color-device-text); }
      .phone-card-header > div { min-width: 0; }
      .phone-card-label { display: block; font-size: var(--text-ui); overflow-wrap: anywhere; }
      .phone-card-serial { display: block; margin-top: 4px; font: var(--text-label) var(--font-mono); overflow-wrap: anywhere; }
      .phone-card-state { display: flex; align-items: center; gap: 4px; margin: 0; padding: 8px 12px; font-size: var(--text-label); color: var(--color-text-muted); }
      .phone-card-state .material-symbols-outlined { font-size: 16px; }
      .chip { width: calc(100% - 16px); min-width: 44px; min-height: 44px; margin: 0 8px 8px; justify-content: center; border: 0; border-radius: var(--radius-md); background: var(--color-primary); color: var(--color-on-primary); font: 500 var(--text-ui) var(--font-ui); }
      .chip .icon, .chip .text { display: none; }
      .phone-card-action { display: inline; }
      .panel { top: auto; bottom: calc(100% + 8px); max-height: calc(100dvh - 96px); overflow: auto; border-radius: var(--radius-lg); box-shadow: var(--shadow-2); }
      .option, .action { min-width: 44px; min-height: 44px; }
    }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class WorkspaceDeviceChipComponent {
  protected readonly phone = inject(WorkspacePhoneService);
  private readonly agent = inject(AgentService);
  private readonly system = inject(SystemService);
  protected readonly deviceLabel = computed(() => {
    const serial = this.phone.target()?.serial;
    if (!serial) return 'No phone';
    const device = this.system.connectedDevices().find(device => device.serial === serial) ?? { serial, model: null };
    const title = deviceTitle(device);
    const kind = deviceKindLabel(device);
    return title === kind ? title : `${title} · ${kind}`;
  });
  protected readonly queuedCount = computed(() => this.agent.sessions().filter(session => {
    const serial = session.device_serial ?? session.device_id;
    return session.status === 'pending' && (!serial || serial === this.phone.target()?.serial);
  }).length);
  protected readonly stateLabel = computed(() => {
    if (this.phone.runActive()) {
      const paused = this.agent.sessions().some(session => session.status === 'paused' &&
        (!(session.device_serial ?? session.device_id) || (session.device_serial ?? session.device_id) === this.phone.target()?.serial));
      return paused ? 'Paused' : 'Running';
    }
    switch (this.phone.view().kind) {
      case 'connected': return 'Connected';
      case 'connecting': return 'Connecting';
      case 'dropped': return 'Disconnected';
      case 'interrupted': return 'Interrupted';
      case 'other-tab': return 'In another tab';
      default: return 'Not connected';
    }
  });
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private readonly chipButton = viewChild.required<ElementRef<HTMLButtonElement>>('chipButton');

  private readonly uid = nextId++;
  protected readonly panelId = `phone-panel-${this.uid}`;
  protected readonly statusId = `phone-status-${this.uid}`;
  protected readonly open = signal(false);
  protected readonly confirmingDisconnect = signal(false);

  constructor() {
    // Run pressed with no phone: open the picker so the next step is obvious.
    let seen = this.phone.pickerRequests();
    effect(() => {
      const requests = this.phone.pickerRequests();
      if (requests !== seen) {
        seen = requests;
        untracked(() => this.openPicker());
      }
    });
    inject(DestroyRef).onDestroy(() => this.open.set(false));
  }

  protected noteId(serial: string): string {
    return `phone-note-${this.uid}-${serial.replace(/[^a-zA-Z0-9_-]/g, '_')}`;
  }

  protected toggle(): void {
    if (this.open()) {
      this.close(false);
    } else {
      this.openPicker();
    }
  }

  private openPicker(): void {
    this.open.set(true);
    // Move focus into the panel once it is rendered, so keyboard users land on the first choice.
    afterNextRender(
      () => this.host.nativeElement.querySelector<HTMLElement>('.panel button:not(:disabled)')?.focus(),
      { injector: this.injector }
    );
  }

  protected close(returnFocus: boolean): void {
    if (!this.open()) return;
    this.open.set(false);
    this.confirmingDisconnect.set(false);
    if (returnFocus) this.chipButton().nativeElement.focus();
  }

  protected onFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget as Node | null;
    if (this.open() && next && !this.host.nativeElement.contains(next)) {
      this.close(false);
    }
  }

  protected choose(serial: string): void {
    this.phone.choose(serial);
    this.close(true);
  }

  protected connect(): void {
    this.close(true);
    void this.phone.connectFromBrowser();
  }

  protected useHere(): void {
    this.close(true);
    void this.phone.useHere();
  }

  protected askDisconnect(): void {
    if (this.phone.runUsesBrowserPhone()) {
      this.confirmingDisconnect.set(true);
    } else {
      this.disconnect();
    }
  }

  protected disconnect(): void {
    this.close(true);
    void this.phone.disconnectBrowserPhone();
  }
}
