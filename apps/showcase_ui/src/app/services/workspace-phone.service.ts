import { computed, effect, inject, Injectable, signal, untracked } from '@angular/core';
import { HostsResponse } from '../core/models/host.model';
import { AgentService } from './agent.service';
import { HostsService } from './hosts.service';
import { PhoneTabService } from './phone-tab.service';
import { SystemService } from './system.service';
import { UsbDeviceRelayService } from './usb-device-relay.service';
import { pickerOptions, WorkspaceChipInput, workspaceChipView } from '../utils/workspace-chip.util';

/**
 * The one phone the Workspace runs on: what the chip shows, what the next run binds to, and
 * the recovery actions any signed-in person may take for their own phone. It never restarts
 * the shared adb server and never changes the server's shared device (`devices/select`).
 */
@Injectable({ providedIn: 'root' })
export class WorkspacePhoneService {
  private readonly relay = inject(UsbDeviceRelayService);
  private readonly system = inject(SystemService);
  private readonly hosts = inject(HostsService);
  private readonly agent = inject(AgentService);
  private readonly tabs = inject(PhoneTabService);

  private readonly registry = signal<HostsResponse | null>(null);
  private readonly pickerRequestCount = signal(0);

  /** Whichever run is live, wherever the person is looking: viewing history changes nothing here. */
  private readonly liveRuns = computed(() =>
    this.agent.sessions().filter((session) => session.status === 'running' || session.status === 'paused')
  );
  private readonly browserSerial = computed(() => {
    const { status, serial } = this.relay.state();
    return status === 'connected' ? serial : null;
  });
  /** A live run counts as on a phone when it names it; a run whose phone is not known yet counts as on ours. */
  private runIsOn(serial: string | null): boolean {
    return (
      serial !== null &&
      this.liveRuns().some((run) => {
        const runSerial = run.device_serial ?? run.device_id ?? null;
        return runSerial === null || runSerial === serial;
      })
    );
  }
  /** A run is live on the phone the next run would use, or on the phone this browser holds. */
  public readonly runActive = computed(() => {
    if (this.liveRuns().length === 0) return ['running', 'paused'].includes(this.agent.agentStatus());
    return this.runIsOn(this.target()?.serial ?? null) || this.runIsOn(this.browserSerial());
  });
  public readonly runInterrupted = computed(
    () => this.agent.currentSession()?.status?.toLowerCase() === 'interrupted'
  );

  private readonly input = computed<WorkspaceChipInput>(() => ({
    relay: this.relay.state(),
    webUsbSupported: this.relay.isSupported(),
    devices: this.system.connectedDevices(),
    registry: this.registry()?.devices ?? [],
    computers: this.registry()?.hosts ?? [],
    selected: this.system.selectedRunTarget(),
    runInterrupted: this.runInterrupted(),
    attaching: this.relay.attaching(),
    otherTab: this.relay.heldInAnotherTab()
  }));

  public readonly view = computed(() => workspaceChipView(this.input()));
  public readonly options = computed(() => pickerOptions(this.input()));
  /** Where the next run goes; null means Run stays blocked. */
  public readonly target = computed(() => this.view().target);
  /** A live run is on the phone this tab holds, so disconnecting it stops that run. */
  public readonly runUsesBrowserPhone = computed(() => this.runIsOn(this.browserSerial()));
  public readonly canConnectFromBrowser = computed(
    () => this.relay.isSupported() && this.relay.state().status !== 'connecting' && !this.runActive()
  );
  public readonly browserPhoneConnected = computed(() => this.relay.state().status === 'connected');
  public readonly connectionError = computed(() => this.relay.state().error);
  /** Counts requests to open the picker (e.g. Run pressed with no phone). */
  public readonly pickerRequests = this.pickerRequestCount.asReadonly();

  constructor() {
    // Source names come from the computers registry; refresh it when the phone list changes.
    effect(() => {
      this.system.connectedDevices();
      untracked(() =>
        this.hosts.list().subscribe({
          next: (response) => this.registry.set(response),
          error: () => this.registry.set(null)
        })
      );
    });

    // A phone the person just connected here is the phone they want to run on. When this tab
    // loses it, forget it: its address is gone and adb may still list it for a moment.
    let announced: string | null = null;
    effect(() => {
      const { status, serial } = this.relay.state();
      const live = status === 'connected' ? serial : null;
      untracked(() => {
        if (live && live !== announced) {
          this.system.chooseRunTarget(live);
        } else if (!live && announced && this.system.selectedRunTarget() === announced) {
          this.system.chooseRunTarget(null);
        }
      });
      announced = live;
    });
  }

  public requestPicker(): void {
    this.pickerRequestCount.update((count) => count + 1);
  }

  /** Connect (or reconnect) a phone from this browser. Reconnecting does not resume a run. */
  public async connectFromBrowser(): Promise<void> {
    await this.relay.connect();
  }

  /** Take the phone over from the other tab that holds it, then connect it here. */
  public async useHere(): Promise<void> {
    await this.tabs.requestRelease();
    await this.relay.connect();
  }

  public async disconnectBrowserPhone(): Promise<void> {
    await this.relay.disconnect();
  }

  public choose(serial: string): void {
    this.system.chooseRunTarget(serial);
  }
}
