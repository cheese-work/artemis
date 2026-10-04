import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { HostsResponse } from '../../core/models/host.model';
import { HostsService } from '../../services/hosts.service';
import { DeviceChipComponent } from '../device-chip/device-chip.component';

/** Phones people can use: shared by a computer or plugged into this browser. Silent when computers are off. */
@Component({
  selector: 'app-registry-phones',
  standalone: true,
  imports: [DeviceChipComponent],
  template: `
    @if (registry(); as current) {
      @if (current.enabled && current.devices.length > 0) {
        <section class="registry-phones" aria-label="Phones shared with SmartQA">
          <h4>Phones shared with SmartQA</h4>
          <ul>
            @for (device of current.devices; track device.computer_id + ':' + device.serial) {
              <li><app-device-chip [device]="device" [computers]="current.hosts" /></li>
            }
          </ul>
        </section>
      }
    }
  `,
  styles: [`
    :host { display: block; }
    .registry-phones { margin-top: 12px; }
    h4 { margin: 0 0 .5rem; font-size: .9rem; }
    ul { display: grid; gap: .5rem; margin: 0; padding: 0; list-style: none; }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class RegistryPhonesComponent implements OnInit {
  private readonly hosts = inject(HostsService);
  private readonly destroyRef = inject(DestroyRef);
  public readonly registry = signal<HostsResponse | null>(null);

  public ngOnInit(): void {
    this.hosts
      .list()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ next: (response) => this.registry.set(response), error: () => this.registry.set(null) });
  }
}
