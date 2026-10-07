import { ChangeDetectionStrategy, Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { HostsResponse } from '../../core/models/host.model';
import { HostsService } from '../../services/hosts.service';
import { SystemService } from '../../services/system.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { deviceSourceOf } from '../../utils/device-chip.util';
import { deviceKindLabel, deviceTitle } from '../../utils/device-label.util';

let nextId = 0;

/**
 * Which phone the next run uses: this person's own phones and shared devices, as the server
 * scoped them. The choice is remembered in this browser only; it never moves the shared
 * diagnostics target, so anyone may use it.
 */
@Component({
  selector: 'app-run-target-picker',
  standalone: true,
  template: `
    @if (options().length > 0) {
      <div class="run-target">
        <label [for]="id">Run on</label>
        <select [id]="id" (change)="choose($any($event.target).value)">
          <option value="" [selected]="current() === ''">Automatic</option>
          @for (option of options(); track option.serial) {
            <option [value]="option.serial" [selected]="option.serial === current()">{{ option.text }}</option>
          }
        </select>
      </div>
    }
  `,
  styles: [`
    .run-target { display: inline-flex; align-items: center; gap: .4rem; font-size: .85rem; color: #475569; }
    select { max-width: 18rem; padding: .2rem .4rem; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; color: #475569; color-scheme: light; }
    option { background: #f8fafc; color: #475569; }
    select:hover, option:hover { background: #f1f5f9; border-color: #cbd5e1; }
    option:checked { background: #eff6ff; color: #1e40af; }
    select:focus-visible { outline: 2px solid #1a73e8; outline-offset: 2px; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunTargetPickerComponent {
  private readonly system = inject(SystemService);
  private readonly hostsService = inject(HostsService);
  private readonly relay = inject(UsbDeviceRelayService);
  private readonly registry = signal<HostsResponse | null>(null);
  protected readonly id = `run-target-${nextId++}`;

  // Source names come from the computers registry; refresh it when the phone list changes.
  private readonly refreshRegistry = effect(() => {
    this.system.connectedDevices();
    untracked(() =>
      this.hostsService.list().subscribe({
        next: (response) => this.registry.set(response),
        error: () => this.registry.set(null)
      })
    );
  });

  protected readonly options = computed(() => {
    const registry = this.registry();
    const relay = this.relay.state();
    return this.system
      .connectedDevices()
      .filter((device) => device.state === 'device')
      .map((device) => {
        const title = deviceTitle(device);
        const kind = deviceKindLabel(device);
        const source = deviceSourceOf(
          device.serial,
          registry?.devices ?? [],
          registry?.hosts ?? [],
          relay.status === 'connected' ? relay.serial : null
        );
        return {
          serial: device.serial,
          text: [title, kind === title ? null : kind, source].filter(Boolean).join(' · ')
        };
      });
  });

  /** The remembered phone while it is still listed; otherwise Automatic. */
  protected readonly current = computed(() => {
    const chosen = this.system.selectedRunTarget();
    return chosen && this.options().some((option) => option.serial === chosen) ? chosen : '';
  });

  protected choose(serial: string): void {
    this.system.chooseRunTarget(serial || null);
  }
}
