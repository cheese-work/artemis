import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Router, RouterLink } from '@angular/router';
import { HostsResponse, RegistryDevice } from '../../core/models/host.model';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { SystemService } from '../../services/system.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { deviceChipView } from '../../utils/device-chip.util';

type PhoneStatus = 'In use' | 'Idle' | 'Disconnected' | 'Needs attention';

const key = (device: RegistryDevice) => `${device.computer_id}:${device.serial}`;

/** Every phone people can use (shared by a computer or plugged into a browser): list on the left, the selected phone on the right. */
@Component({
  selector: 'app-phones',
  standalone: true,
  imports: [RouterLink],
  template: `
    <div class="phones">
      <section class="list" aria-labelledby="phones-title">
        <header>
          <h2 id="phones-title">Phones</h2>
          <button type="button" class="primary" [disabled]="!phone.canConnectFromBrowser()" (click)="phone.connectFromBrowser()">Connect</button>
        </header>
        @if (failed()) {
          <p class="note" role="alert">Phones could not be loaded. <button type="button" (click)="load()">Retry</button>.</p>
        } @else if (registry(); as current) {
          @for (device of current.devices; track key(device)) {
            <button type="button" class="phone-row" [attr.aria-current]="device === selected() ? 'true' : null" (click)="selectedKey.set(key(device))">
              <span class="dot" [class]="statusOf(device).replace(' ', '-').toLowerCase()" aria-hidden="true"></span>
              <span class="who">
                <span class="phone-name">{{ view(device).label }}</span>
                <span class="phone-source">{{ view(device).source }}</span>
                <small class="phone-serial">{{ device.serial }}</small>
              </span>
              <span class="phone-status">{{ statusOf(device) }}</span>
            </button>
          } @empty {
            <p class="note">No phones yet. Connect one by USB or start a Cloud emulator.</p>
          }
        }
      </section>
      @if (selected(); as device) {
        @let v = view(device);
        @let live = liveSession(device);
        <section class="phone-detail" aria-label="Selected phone">
          <h3>{{ v.label }}</h3>
          <div class="serial">{{ device.serial }}</div>
          <div class="acts">
            <span class="state">{{ statusOf(device) }}</span>
            @if (live) { <a [routerLink]="['/sessions', live.session_id]">Open live session</a> }
            <button type="button" (click)="newSession(device)">New session on this phone</button>
          </div>
          <dl>
            <dt>Source</dt><dd>{{ v.source }}. {{ v.detail }}</dd>
            @if (live) { <dt>In use by</dt><dd>{{ live.initial_goal }}</dd> }
            <dt>Owner</dt><dd>{{ device.owner || device.computer_name || 'Not recorded' }}</dd>
          </dl>
        </section>
      }
    </div>
  `,
  styles: `
    :host { display: block; }
    .phones { display: grid; grid-template-columns: 340px minmax(0, 1fr); min-height: 420px; color: var(--color-text); }
    .list { border-right: 1px solid var(--color-rule); }
    header { display: flex; align-items: center; gap: var(--space-smd); padding: var(--space-md) var(--space-lg); }
    h2 { flex: 1; margin: 0; font-size: var(--text-title); color: var(--color-ink); }
    button, .acts a { display: inline-flex; align-items: center; min-height: 44px; padding: 0 var(--space-md); border: 1px solid var(--color-rule-strong); border-radius: var(--radius-md); background: var(--color-surface); color: var(--color-text); font: inherit; cursor: pointer; text-decoration: none; }
    button.primary { background: var(--color-primary); border-color: var(--color-primary); color: var(--color-on-primary); }
    button:disabled { opacity: .5; cursor: not-allowed; }
    button:focus-visible, a:focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }
    .phone-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: var(--space-smd); align-items: center; width: 100%; min-height: 56px; padding: 0 var(--space-lg); border: 0; border-top: 1px solid var(--color-rule); border-radius: 0; text-align: left; }
    .phone-row[aria-current=true] { background: var(--color-primary-tint); box-shadow: inset 3px 0 0 var(--color-primary); }
    .who { display: grid; min-width: 0; }
    .phone-name { color: var(--color-ink); font-weight: 500; overflow-wrap: anywhere; }
    .phone-source { font-size: var(--text-label); color: var(--color-text-muted); }
    .phone-serial, .serial { font: var(--text-label) var(--font-mono); color: var(--color-text-faint); overflow-wrap: anywhere; }
    .phone-status { font-size: var(--text-label); color: var(--color-text-muted); }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--color-text-faint); }
    .dot.in-use { background: var(--color-success); }
    .dot.disconnected, .dot.needs-attention { background: var(--color-error-solid); }
    .note { margin: 0; padding: var(--space-md) var(--space-lg); color: var(--color-text-muted); }
    .phone-detail { padding: var(--space-lg); }
    h3 { margin: 0 0 var(--space-xs); font-size: var(--text-section); color: var(--color-ink); }
    .acts { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-smd); margin: var(--space-md) 0; }
    .state { font-weight: 600; }
    dl { display: grid; grid-template-columns: max-content 1fr; gap: var(--space-xs) var(--space-md); margin: 0; font-size: var(--text-label); color: var(--color-text-muted); }
    dt { font-weight: 600; color: var(--color-text); }
    dd { margin: 0; overflow-wrap: anywhere; }
    @media (max-width: 800px) {
      .phones { grid-template-columns: 1fr; }
      .list { border-right: 0; }
      .phone-detail { border-top: 1px solid var(--color-rule); }
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class PhonesComponent {
  private readonly hosts = inject(HostsService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly agent = inject(AgentService);
  private readonly system = inject(SystemService);
  private readonly router = inject(Router);
  private readonly relay = inject(UsbDeviceRelayService);
  protected readonly phone = inject(WorkspacePhoneService);
  protected readonly registry = signal<HostsResponse | null>(null);
  protected readonly failed = signal(false);
  protected readonly selectedKey = signal<string | null>(null);
  protected readonly selected = computed(() => {
    const devices = this.registry()?.devices ?? [];
    return devices.find(device => key(device) === this.selectedKey()) ?? devices[0] ?? null;
  });
  protected readonly key = key;

  constructor() { this.load(); }

  protected load(): void {
    this.failed.set(false);
    this.hosts.list().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: response => this.registry.set(response),
      error: () => this.failed.set(true)
    });
  }

  protected view(device: RegistryDevice) {
    return deviceChipView(device, this.registry()?.hosts ?? [], this.relay.state().serial);
  }

  protected liveSession(device: RegistryDevice) {
    return this.agent.sessions().find(session =>
      (session.status === 'running' || session.status === 'paused') && (session.device_serial ?? session.device_id) === device.serial);
  }

  protected statusOf(device: RegistryDevice): PhoneStatus {
    const { state } = this.view(device);
    if (state === 'Offline') return 'Disconnected';
    if (state === 'Needs attention') return state;
    return this.liveSession(device) ? 'In use' : 'Idle';
  }

  protected newSession(device: RegistryDevice): void {
    this.system.chooseRunTarget(device.serial);
    void this.router.navigateByUrl('/sessions/new');
  }
}
