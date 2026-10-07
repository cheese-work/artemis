import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { LabelableDevice, runDeviceLabel } from '../../utils/device-label.util';

@Component({
  selector: '[appRunDeviceLabel]',
  standalone: true,
  template: `
    @if (detail()) { {{ label() }} } @else {
      <span class="material-symbols-outlined device-icon">phone_android</span>
      <span class="device-name">{{ label() }}</span>
    }
  `,
  styles: [`
    .device-icon { font-size: 12px; width: 12px; height: 12px; line-height: 12px; color: #64748b; opacity: 0.75; flex-shrink: 0; }
    .device-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; font-family: inherit; letter-spacing: -0.15px; }
    :host(:hover) .device-icon { color: #334155; opacity: 1; }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunDeviceLabelComponent {
  readonly serial = input<string | null>(null);
  readonly device = input<LabelableDevice | null>(null);
  readonly ownBrowser = input(false);
  readonly detail = input(false);
  readonly label = computed(() => runDeviceLabel(this.serial(), this.device(), this.ownBrowser(), this.detail()));
}
