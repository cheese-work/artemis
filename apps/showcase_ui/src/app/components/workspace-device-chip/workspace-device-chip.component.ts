import { ChangeDetectionStrategy, Component, DestroyRef, ElementRef, Injector, afterNextRender, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';

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
      border: 1px solid #cbd5e1; border-radius: 22px; background: rgba(255, 255, 255, .85); color: #0f172a;
      font-size: .85rem; font-weight: 600;
    }
    .chip .text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chip.connected { border-color: #86efac; }
    .chip.connected .icon { color: #16a34a; }
    .chip.dropped, .chip.interrupted, .chip.other-tab { border-color: #fcd34d; background: #fffbeb; }
    .chip.dropped .icon, .chip.interrupted .icon, .chip.other-tab .icon { color: #b45309; }
    .chip.none .icon { color: #64748b; }
    .spinning { animation: chip-spin 1s linear infinite; }
    @keyframes chip-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .spinning { animation: none; } }
    .panel {
      position: absolute; top: calc(100% + 8px); left: 0; z-index: 60; width: 21rem; max-width: calc(100vw - 2rem);
      display: flex; flex-direction: column; gap: .5rem; padding: .75rem; border: 1px solid #e2e8f0; border-radius: 14px;
      background: #fff; color: #0f172a; box-shadow: 0 12px 32px -8px rgba(15, 23, 42, .25);
    }
    .hint, .empty, .note { margin: 0; font-size: .85rem; color: #475569; }
    .error { margin: 0; font-size: .85rem; color: #b91c1c; white-space: pre-line; }
    .options { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: .25rem; }
    .option, .action {
      display: flex; align-items: center; justify-content: space-between; gap: .5rem; width: 100%; padding: 0 .75rem;
      border: 1px solid #cbd5e1; border-radius: 10px; background: #f8fafc; color: inherit; font-size: .9rem; text-align: left;
    }
    .option[aria-pressed='true'] { border-color: #16a34a; background: #f0fdf4; }
    a.action { text-decoration: none; }
    .option:disabled, .action:disabled { opacity: .55; cursor: not-allowed; }
    .action { justify-content: center; }
    .action.danger { border-color: #fca5a5; color: #b91c1c; }
    .confirm { display: flex; gap: .5rem; }
    button:focus-visible { outline: 3px solid #2563eb; outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class WorkspaceDeviceChipComponent {
  protected readonly phone = inject(WorkspacePhoneService);
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
