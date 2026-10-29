import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, DestroyRef, NgZone, computed, effect, inject, signal, untracked } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { Computer, ComputerStatus, DeviceLease, HostsResponse, RegistryDevice } from '../../core/models/host.model';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { SystemService } from '../../services/system.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { deviceChipView } from '../../utils/device-chip.util';
import { serialShape } from '../../utils/device-label.util';

type PhoneStatus = 'Held' | 'Available' | 'Disconnected' | 'Needs attention';

const LEASE_ERRORS = new Map([
  ['device_leases_disabled', 'Phone borrowing is turned off on this server. Ask an administrator to turn it on.'],
  ['device_unknown', 'SmartQA does not know this phone yet. Reload the page; if it stays, reconnect the phone.'],
  ['device_store_not_ready', 'Phone identity is not ready. Try again in a minute.']
]);

const key = (device: RegistryDevice) => `${device.computer_id}:${device.serial}`;

/** One phone and every way it is connected: the server names them with one device id. */
interface PhoneGroup { device: RegistryDevice; endpoints: RegistryDevice[]; }

@Component({
  selector: 'app-phones',
  standalone: true,
  imports: [RouterLink],
  template: `
    <main class="page" aria-labelledby="page-title">
      <header class="heading">
        <div>
          <div class="eyebrow">Inventory</div>
          <h1 id="page-title">Phones and computers</h1>
          <p>See phone availability and the computers that share them with SmartQA.</p>
        </div>
        <div class="top-actions">
          <button type="button" class="button secondary" [disabled]="!phone.canConnectFromBrowser()" (click)="phone.connectFromBrowser()">
            <span class="material-symbols-outlined" aria-hidden="true">usb</span>Connect
          </button>
          <button type="button" class="button primary" (click)="focusBorrowForm()">
            <span class="material-symbols-outlined" aria-hidden="true">devices</span>Request a phone
          </button>
        </div>
      </header>

      @if (phone.connectionError(); as error) {
        <p class="notice error" role="alert">{{ error }}</p>
      }
      @if (failed()) {
        <p class="notice error" role="alert">Phones could not be loaded. <button type="button" class="text-button" (click)="load()">Retry</button>.</p>
      }
      @if (!registry() && !failed()) {
        <section class="panel loading" aria-label="Loading phones" aria-busy="true">
          <span class="material-symbols-outlined spinning" aria-hidden="true">progress_activity</span>
          <span>Loading phones and computers…</span>
        </section>
      }
      @if (registry(); as current) {
        @if (!current.enabled) {
          <p class="notice">Phone sharing is disabled on this server. Ask an administrator to enable computer sharing.</p>
        }
        <section class="inventory-summary" aria-label="Inventory summary">
          <span class="summary-icon"><span class="material-symbols-outlined" aria-hidden="true">smartphone</span></span>
          <div>
            <strong>{{ phones().length }} phones · {{ current.hosts.length }} computers</strong>
            <p>Phone availability updates from registered computers and browser connections.</p>
          </div>
        </section>

        <div class="device-layout">
          <section class="panel inventory-panel" aria-labelledby="phones-title">
            <header class="panel-head">
              <h2 id="phones-title">Phones</h2>
              <span class="tag">{{ phones().length }} {{ phones().length === 1 ? 'phone' : 'phones' }}</span>
            </header>
            <div class="phone-list">
              @for (group of phones(); track key(group.device)) {
                @let device = group.device;
                @let v = view(device);
                @let live = liveSession(group);
                @let status = statusOf(group);
                <article class="phone-card">
                  <div class="phone-card-main">
                    <div class="phone-name">
                      <span class="device-icon"><span class="material-symbols-outlined" aria-hidden="true">smartphone</span></span>
                      <strong>{{ v.label }}</strong>
                    </div>
                    <span class="status" [class]="'status ' + phoneTone(status)">
                      <span class="material-symbols-outlined" aria-hidden="true">{{ phoneIcon(status) }}</span>{{ status }}
                    </span>
                  </div>
                  <div class="phone-details">
                    <p>{{ v.source }}<span aria-hidden="true"> · </span><span>{{ v.detail }}</span></p>
                    <div class="endpoint-list" aria-label="Phone connections">
                      @for (endpoint of group.endpoints; track key(endpoint)) {
                        <div class="endpoint">
                          <strong>{{ endpointLabel(endpoint) }}</strong>
                          <span class="mono phone-serial">{{ endpoint.serial }}</span>
                        </div>
                      }
                    </div>
                  </div>
                  @if (leaseOf(group); as lease) {
                    <p class="lease-details"><strong>Leased by {{ lease.holder }}:</strong> {{ lease.purpose || 'No purpose given' }}, until {{ leaseEnd(lease) }}</p>
                  }
                  @if (live) {
                    <p class="lease-details"><strong>Active session:</strong> {{ live.initial_goal || 'Session in progress' }}</p>
                    <a class="session-link" [routerLink]="['/sessions', live.session_id]">Open live session</a>
                  }
                  <div class="phone-actions">
                    <button type="button" class="button secondary" [disabled]="status !== 'Available'" (click)="newSession(group)">New session on this phone</button>
                  </div>
                </article>
              } @empty {
                <div class="empty-state">
                  <span class="material-symbols-outlined" aria-hidden="true">devices</span>
                  <strong>No phones are enrolled</strong>
                  <p>Connect one by USB or start a Cloud emulator.</p>
                </div>
              }
            </div>

            <section class="computers" aria-labelledby="computers-title">
              <header class="panel-head">
                <h2 id="computers-title">Computers</h2>
                <span class="tag">{{ current.hosts.length }} enrolled</span>
              </header>
              <div class="computer-list">
                @for (host of current.hosts; track host.id) {
                  <article class="computer-card">
                    <div class="computer-title">
                      <strong>{{ host.name }}</strong>
                      <span class="status" [class]="'status ' + computerTone(host.status)">
                        <span class="material-symbols-outlined" aria-hidden="true">{{ computerIcon(host.status) }}</span>{{ computerLabel(host.status) }}
                      </span>
                    </div>
                    <p>
                      {{ host.os || 'Operating system not reported' }}<span aria-hidden="true"> · </span>
                      {{ host.phones_shared }} phones shared
                    </p>
                    @if (host.reason) { <p class="computer-reason">{{ host.reason }}</p> }
                  </article>
                } @empty {
                  <p class="empty-computers">No computers are enrolled.</p>
                }
              </div>
            </section>
          </section>

          <section class="panel borrow-panel" id="borrow-panel" aria-labelledby="borrow-title">
            <header class="panel-head">
              <h2 id="borrow-title">Borrow a phone</h2>
              <span class="tag">Lease request</span>
            </header>
            <div class="panel-body">
              <p class="borrow-intro">Choose an available phone, state the purpose, then set how long you need it.</p>
              @if (current.enabled) {
                <form aria-label="Borrow a phone" (submit)="requestLease($event)">
                  <div class="field">
                    <label for="phone-choice">Phone</label>
                    <select id="phone-choice" [value]="selectedLeaseDevice() ? key(selectedLeaseDevice()!.device) : ''" [disabled]="!availablePhones().length || requesting()" (change)="selectLeasePhone($event)">
                      @for (group of availablePhones(); track key(group.device)) {
                        <option [value]="key(group.device)">{{ view(group.device).label }}, {{ view(group.device).source }}</option>
                      } @empty {
                        <option value="">No available phones</option>
                      }
                    </select>
                  </div>
                  <div class="field">
                    <label for="purpose">Purpose</label>
                    <input id="purpose" [value]="purpose()" (input)="setPurpose($event)" maxlength="160" required>
                  </div>
                  <div class="field">
                    <label for="duration">Duration</label>
                    <select id="duration" [value]="duration()" (change)="setDuration($event)" [disabled]="requesting()">
                      <option value="1800">30 minutes</option>
                      <option value="3600">1 hour</option>
                      <option value="7200">2 hours</option>
                    </select>
                  </div>
                  <button type="submit" class="button primary submit" [disabled]="!current.enabled || !selectedLeaseDevice() || !purpose().trim() || requesting()">
                    @if (requesting()) { Requesting… } @else { Request lease }
                  </button>
                </form>
                @if (!availablePhones().length) {
                  <p class="form-help">No available phones right now. Disconnected or in-use phones cannot be requested.</p>
                }
              } @else {
                <p class="form-help">Phone sharing is disabled. Lease requests are unavailable.</p>
              }
              @if (leaseMessage()) { <p class="notice success" role="status" aria-live="polite">{{ leaseMessage() }}</p> }
              @if (leaseError()) { <p class="notice error" role="alert">{{ leaseError() }}</p> }
              <div class="borrow-steps" aria-label="Borrow flow: phone, purpose, duration, queue, decision">
                <div class="borrow-step"><span class="material-symbols-outlined" aria-hidden="true">smartphone</span>Phone</div>
                <div class="borrow-step"><span class="material-symbols-outlined" aria-hidden="true">description</span>Purpose</div>
                <div class="borrow-step"><span class="material-symbols-outlined" aria-hidden="true">schedule</span>Duration</div>
                <div class="borrow-step"><span class="material-symbols-outlined" aria-hidden="true">hourglass_empty</span>Queue</div>
                <div class="borrow-step"><span class="material-symbols-outlined" aria-hidden="true">check_circle</span>Decision</div>
              </div>
            </div>
          </section>
        </div>
      }
    </main>
  `,
  styles: `
    :host { display: block; }
    .page { display: grid; gap: var(--space-md); max-width: 1440px; margin: 0 auto; padding: var(--space-lg); color: var(--color-text); }
    .heading { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--space-lg); }
    .eyebrow { margin-bottom: var(--space-xs); color: var(--color-text-muted); font-size: var(--text-label); font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
    h1, h2, p { margin: 0; }
    h1 { color: var(--color-ink); font-size: var(--text-title); line-height: 1.2; }
    h2 { color: var(--color-ink); font-size: var(--text-section); }
    .heading p { margin-top: var(--space-xs); color: var(--color-text-muted); }
    .top-actions, .phone-actions { display: flex; flex-wrap: wrap; gap: var(--space-sm); }
    .button, .text-button { display: inline-flex; align-items: center; justify-content: center; gap: var(--space-xs); min-height: 44px; padding: 0 var(--space-md); border: 1px solid var(--color-rule-strong); border-radius: var(--radius-md); background: var(--color-surface); color: var(--color-text); font: inherit; cursor: pointer; text-decoration: none; }
    .button .material-symbols-outlined { font-size: 18px; }
    .button.primary { border-color: var(--color-primary); background: var(--color-primary); color: var(--color-on-primary); }
    .button:disabled { opacity: .52; cursor: not-allowed; }
    .text-button { min-height: auto; padding: 0; border: 0; color: var(--color-primary); text-decoration: underline; }
    .panel { min-width: 0; overflow: hidden; border: 1px solid var(--color-rule); border-radius: var(--radius-md); background: var(--color-surface); }
    .panel-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-sm); padding: var(--space-md); border-bottom: 1px solid var(--color-rule); }
    .panel-body { display: grid; gap: var(--space-md); padding: var(--space-md); }
    .tag { flex: none; padding: 3px 9px; border-radius: 999px; background: var(--color-surface-subtle); color: var(--color-text-muted); font-size: var(--text-label); font-weight: 600; }
    .inventory-summary { display: flex; align-items: center; gap: var(--space-md); padding: var(--space-md); border: 1px solid var(--color-rule); border-radius: var(--radius-md); background: var(--color-surface); }
    .summary-icon, .device-icon { display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: var(--radius-md); background: var(--color-ink); color: var(--color-surface); }
    .summary-icon .material-symbols-outlined { font-size: 22px; }
    .inventory-summary strong { color: var(--color-ink); }
    .inventory-summary p { margin-top: 3px; color: var(--color-text-muted); }
    .device-layout { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(320px, .8fr); align-items: start; gap: var(--space-md); }
    .phone-list { display: grid; gap: var(--space-sm); padding: var(--space-sm); }
    .phone-card { display: grid; gap: var(--space-sm); min-width: 0; padding: var(--space-md); border: 1px solid var(--color-rule); border-radius: var(--radius-md); background: var(--color-surface); }
    .phone-card-main, .phone-name, .computer-title { display: flex; align-items: center; justify-content: space-between; gap: var(--space-sm); min-width: 0; }
    .phone-name { justify-content: flex-start; color: var(--color-ink); }
    .phone-name strong { overflow-wrap: anywhere; }
    .device-icon { width: 36px; height: 40px; }
    .device-icon .material-symbols-outlined { font-size: 20px; }
    .phone-details p, .computer-card p { color: var(--color-text-muted); font-size: var(--text-label); overflow-wrap: anywhere; }
    .endpoint-list { display: grid; margin-top: var(--space-sm); border-top: 1px solid var(--color-rule); }
    .endpoint { display: grid; grid-template-columns: minmax(100px, .7fr) minmax(0, 1fr) auto; align-items: center; gap: var(--space-sm); padding-top: var(--space-sm); }
    .endpoint strong { font-size: var(--text-label); font-weight: 600; }
    .mono { font: var(--text-label) var(--font-mono); overflow-wrap: anywhere; }
    .status { display: inline-flex; align-items: center; justify-content: center; gap: 4px; width: fit-content; padding: 3px 9px; border-radius: 999px; font-size: var(--text-label); font-weight: 650; white-space: nowrap; }
    .status .material-symbols-outlined { font-size: 15px; }
    .status.pass { background: var(--color-success-bg); color: var(--color-success); }
    .status.warn { background: var(--color-warning-bg); color: var(--color-warning); }
    .status.fail { background: var(--color-error-bg); color: var(--color-error); }
    .status.neutral { background: var(--color-surface-subtle); color: var(--color-text-muted); }
    .lease-details { color: var(--color-text-muted); font-size: var(--text-label); overflow-wrap: anywhere; }
    .lease-details strong { color: var(--color-ink); }
    .session-link { width: fit-content; color: var(--color-primary); font-size: var(--text-label); }
    .phone-actions { padding-top: var(--space-xs); border-top: 1px solid var(--color-rule); }
    .computers { border-top: 1px solid var(--color-rule); }
    .computers .panel-head { border-bottom: 1px solid var(--color-rule); }
    .computer-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-sm); padding: var(--space-sm); }
    .computer-card { display: grid; align-content: start; gap: var(--space-xs); min-width: 0; padding: var(--space-sm); border: 1px solid var(--color-rule); border-radius: var(--radius-md); }
    .computer-title strong { min-width: 0; color: var(--color-ink); overflow-wrap: anywhere; }
    .computer-card p { line-height: 1.5; }
    .computer-reason { color: var(--color-error) !important; }
    .empty-computers { grid-column: 1 / -1; padding: var(--space-sm); color: var(--color-text-muted); }
    .empty-state { display: grid; justify-items: center; gap: var(--space-xs); padding: var(--space-lg); color: var(--color-text-muted); text-align: center; }
    .empty-state .material-symbols-outlined { font-size: 26px; }
    .empty-state strong { color: var(--color-ink); }
    .borrow-intro, .form-help { color: var(--color-text-muted); line-height: 1.5; }
    .borrow-panel form { display: grid; gap: var(--space-sm); }
    .field { display: grid; gap: 6px; }
    .field label { color: var(--color-ink); font-size: var(--text-label); font-weight: 650; }
    .field select, .field input { width: 100%; min-height: 44px; padding: 0 var(--space-sm); border: 1px solid var(--color-rule-strong); border-radius: var(--radius-md); background: var(--color-surface); color: var(--color-text); font: inherit; }
    .field select:disabled { color: var(--color-text-muted); }
    .submit { width: 100%; }
    .borrow-steps { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 5px; margin-top: var(--space-sm); }
    .borrow-step { display: grid; justify-items: center; gap: 4px; min-width: 0; padding: var(--space-sm) 2px; border: 1px solid var(--color-rule); border-radius: var(--radius-md); color: var(--color-text-muted); font-size: 11px; text-align: center; }
    .borrow-step .material-symbols-outlined { font-size: 16px; }
    .notice { padding: var(--space-sm) var(--space-md); border: 1px solid var(--color-rule); border-radius: var(--radius-md); background: var(--color-surface); color: var(--color-text-muted); line-height: 1.5; }
    .notice.error { border-color: var(--color-error-bg); color: var(--color-error); }
    .notice.success { border-color: var(--color-success-bg); color: var(--color-success); }
    .loading { display: flex; align-items: center; gap: var(--space-sm); min-height: 100px; padding: var(--space-md); color: var(--color-text-muted); }
    .spinning { animation: spin 1.2s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @media (max-width: 900px) {
      .page { padding: var(--space-md); }
      .device-layout { grid-template-columns: minmax(0, 1fr); }
    }
    @media (max-width: 600px) {
      .page { gap: var(--space-sm); padding: var(--space-sm); }
      .heading { display: grid; gap: var(--space-sm); }
      h1 { font-size: var(--text-title); }
      .top-actions { display: grid; grid-template-columns: 1fr 1fr; }
      .button { padding: 0 var(--space-sm); }
      .computer-list { grid-template-columns: minmax(0, 1fr); }
      .endpoint { grid-template-columns: minmax(0, 1fr) auto; }
      .endpoint strong { grid-column: 1 / -1; }
      .borrow-steps { gap: 3px; }
      .borrow-step { font-size: 10px; }
    }
    @media (prefers-reduced-motion: reduce) {
      .spinning { animation: none; }
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class PhonesComponent {
  private readonly hosts = inject(HostsService);
  private readonly agent = inject(AgentService);
  private readonly system = inject(SystemService);
  private readonly router = inject(Router);
  private readonly zone = inject(NgZone);
  private readonly relay = inject(UsbDeviceRelayService);
  protected readonly phone = inject(WorkspacePhoneService);
  protected readonly registry = signal<HostsResponse | null>(null);
  protected readonly failed = signal(false);
  protected readonly purpose = signal('');
  protected readonly duration = signal(1800);
  protected readonly leaseDeviceKey = signal<string | null>(null);
  protected readonly requesting = signal(false);
  protected readonly leaseMessage = signal<string | null>(null);
  protected readonly leaseError = signal<string | null>(null);
  /** Bumped when a lease expires so `leaseOf` and everything derived from it re-evaluate. */
  private readonly now = signal(Date.now());
  private readonly expiryTimers = new Set<ReturnType<typeof setTimeout>>();
  /** Endpoints sharing a device id are one phone; a phone the server has not matched stays alone. */
  protected readonly phones = computed<PhoneGroup[]>(() => {
    const byId = new Map<string, RegistryDevice[]>();
    const groups: RegistryDevice[][] = [];
    for (const device of this.registry()?.devices ?? []) {
      const shared = device.device_id ? byId.get(device.device_id) : undefined;
      if (shared) { shared.push(device); continue; }
      const group = [device];
      groups.push(group);
      if (device.device_id) byId.set(device.device_id, group);
    }
    // Show the connected endpoint: it is the one a session can start on.
    return groups.map(endpoints => ({ endpoints, device: endpoints.find(e => this.view(e).state !== 'Offline') ?? endpoints[0] }));
  });
  protected readonly availablePhones = computed(() => this.phones().filter(group => !!group.device.device_id && this.statusOf(group) === 'Available'));
  protected readonly selectedLeaseDevice = computed(() => {
    const available = this.availablePhones();
    return available.find(group => key(group.device) === this.leaseDeviceKey()) ?? available[0] ?? null;
  });
  protected readonly key = key;

  private request?: Subscription;
  private leaseRequest?: Subscription;

  constructor() {
    effect(() => {
      this.relay.state().serial;
      this.system.connectedDevices();
      untracked(() => this.load());
    });
    inject(DestroyRef).onDestroy(() => {
      this.request?.unsubscribe();
      this.leaseRequest?.unsubscribe();
      this.expiryTimers.forEach(clearTimeout);
    });
  }

  protected load(): void {
    this.failed.set(false);
    this.request?.unsubscribe();
    this.request = this.hosts.list().subscribe({
      next: response => {
        this.registry.set(response);
        response.devices.forEach(device => device.lease && this.expireAt(device.lease));
      },
      error: () => this.failed.set(true)
    });
  }

  protected view(device: RegistryDevice) {
    return deviceChipView(device, this.registry()?.hosts ?? [], this.relay.state().serial);
  }

  protected liveSession(group: PhoneGroup) {
    return this.agent.sessions().find(session =>
      (session.status === 'running' || session.status === 'paused') && group.endpoints.some(e => (session.device_serial ?? session.device_id) === e.serial));
  }

  protected statusOf(group: PhoneGroup): PhoneStatus {
    const { state } = this.view(group.device);
    if (state === 'Offline') return 'Disconnected';
    if (state === 'Needs attention') return state;
    return this.liveSession(group) || this.leaseOf(group) ? 'Held' : 'Available';
  }

  /** The server sends the lease on every endpoint of a leased phone, whoever holds it. */
  protected leaseOf(group: PhoneGroup): DeviceLease | null {
    const lease = group.endpoints.find(e => e.lease)?.lease;
    return lease && lease.expires_at * 1000 > this.now() ? lease : null;
  }

  protected endpointLabel(endpoint: RegistryDevice): string {
    if (endpoint.source === 'browser') return 'Browser connection';
    return serialShape(endpoint.serial) === 'network' ? 'Wi-Fi' : 'USB';
  }

  protected leaseEnd(lease: DeviceLease): string {
    return new Date(lease.expires_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  protected phoneTone(status: PhoneStatus): string {
    if (status === 'Available') return 'pass';
    if (status === 'Held') return 'warn';
    if (status === 'Disconnected') return 'fail';
    return 'neutral';
  }

  protected phoneIcon(status: PhoneStatus): string {
    if (status === 'Available') return 'check_circle';
    if (status === 'Held') return 'lock';
    if (status === 'Disconnected') return 'error';
    return 'info';
  }

  protected computerLabel(status: ComputerStatus): string {
    const labels: Record<ComputerStatus, string> = {
      online: 'Online', reconnecting: 'Reconnecting', offline: 'Disconnected',
      update_required: 'Update required', revoked: 'Revoked'
    };
    return labels[status];
  }

  protected computerTone(status: ComputerStatus): string {
    if (status === 'online') return 'pass';
    if (status === 'reconnecting' || status === 'update_required') return 'warn';
    if (status === 'offline' || status === 'revoked') return 'fail';
    return 'neutral';
  }

  protected computerIcon(status: ComputerStatus): string {
    if (status === 'online') return 'check_circle';
    if (status === 'reconnecting') return 'sync';
    if (status === 'update_required') return 'system_update';
    return 'error';
  }

  protected selectLeasePhone(event: Event): void {
    this.leaseDeviceKey.set((event.target as HTMLSelectElement).value || null);
    this.leaseMessage.set(null);
    this.leaseError.set(null);
  }

  protected setPurpose(event: Event): void {
    this.purpose.set((event.target as HTMLInputElement).value);
    this.leaseMessage.set(null);
    this.leaseError.set(null);
  }

  protected setDuration(event: Event): void {
    this.duration.set(Number((event.target as HTMLSelectElement).value));
  }

  protected requestLease(event: Event): void {
    event.preventDefault();
    const group = this.selectedLeaseDevice();
    const purpose = this.purpose().trim();
    const ttl = this.duration();
    if (!this.registry()?.enabled || !group || !purpose || this.requesting()) return;
    this.leaseMessage.set(null);
    this.leaseError.set(null);
    this.requesting.set(true);
    this.leaseRequest?.unsubscribe();
    this.leaseRequest = this.hosts.requestLease(group.device.device_id!, ttl, purpose).subscribe({
      next: () => {
        this.requesting.set(false);
        this.leaseMessage.set('Lease request submitted.');
        this.load();
      },
      error: error => {
        this.requesting.set(false);
        this.leaseError.set(this.leaseFailure(error));
      }
    });
  }

  private expireAt(lease: DeviceLease): void {
    this.now.set(Date.now());
    // Outside the zone: an hour-long timer would otherwise keep the app "unstable". The signal still repaints.
    const timer = this.zone.runOutsideAngular(() => setTimeout(() => {
      this.expiryTimers.delete(timer);
      this.now.set(Date.now());
    }, Math.max(lease.expires_at * 1000 - Date.now(), 0) + 1));
    this.expiryTimers.add(timer);
  }

  protected focusBorrowForm(): void {
    document.getElementById('phone-choice')?.focus();
  }

  protected newSession(group: PhoneGroup): void {
    this.system.chooseRunTarget(group.device.serial);
    void this.router.navigateByUrl('/sessions/new');
  }

  private leaseFailure(error: unknown): string {
    if (error instanceof HttpErrorResponse && error.status === 409) {
      const body = error.error as { holder?: unknown; detail?: { holder?: unknown } } | null;
      const holderDetails = body?.holder;
      const holder = typeof holderDetails === 'string'
        ? holderDetails
        : holderDetails !== null && typeof holderDetails === 'object' && 'holder' in holderDetails
          ? holderDetails.holder
          : body?.detail?.holder;
      return typeof holder === 'string' && holder.trim()
        ? `Phone is already held by ${holder}. Choose another phone.`
        : 'This phone is already held. Choose another phone.';
    }
    const code = error instanceof HttpErrorResponse ? (error.error as { code?: unknown } | null)?.code : undefined;
    return (typeof code === 'string' ? LEASE_ERRORS.get(code) : undefined) ?? 'Lease request failed. Retry the request.';
  }
}
