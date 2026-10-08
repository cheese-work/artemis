import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { Computer, RegistryDevice } from '../../core/models/host.model';
import { deviceChipView } from '../../utils/device-chip.util';

/** One phone: primary state, the computer (or browser) it comes from as secondary text. */
@Component({
  selector: 'app-device-chip',
  standalone: true,
  template: `
    @let v = view();
    <span class="chip" [class.ready]="v.state === 'Ready'" [class.offline]="v.state === 'Offline'" [class.attention]="v.state === 'Needs attention'">
      <span class="material-symbols-outlined" aria-hidden="true">{{ v.icon }}</span>
      <span class="label">{{ v.label }}</span>
      @if (v.kind) {
        <span class="kind">{{ v.kind }}</span>
      }
      <span class="state">{{ v.state }}</span>
      <span class="source">{{ v.source }}</span>
    </span>
    <span class="detail">{{ v.detail }} · {{ v.serial }}</span>
  `,
  styles: [`
    :host { display: flex; flex-wrap: wrap; align-items: center; gap: .25rem .75rem; }
    .chip { display: inline-flex; align-items: center; gap: .4rem; padding: .25rem .65rem; border: 1px solid #385174; border-radius: 999px; background: #111d31; color: #e8eef8; }
    .chip.ready { border-color: #3d9a6b; }
    .chip.offline { border-color: #8892a6; }
    .chip.attention { border-color: #d9a441; }
    .kind { color: #a7b4c8; font-size: .85rem; }
    .state { font-weight: 700; }
    .source { color: #a7b4c8; }
    .detail { color: #a7b4c8; font-size: .85rem; }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class DeviceChipComponent {
  public readonly device = input.required<RegistryDevice>();
  public readonly computers = input<Computer[]>([]);
  /** The bridge serial this browser tab holds, if any. */
  public readonly ownBrowserSerial = input<string | null>(null);
  public readonly view = computed(() =>
    deviceChipView(this.device(), this.computers(), this.ownBrowserSerial())
  );
}
