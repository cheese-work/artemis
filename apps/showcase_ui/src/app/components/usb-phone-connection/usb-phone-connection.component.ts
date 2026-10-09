import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';

@Component({
  selector: 'app-usb-phone-connection',
  standalone: true,
  template: `
    <section class="usb-phone-connect" [attr.aria-busy]="relay.state().status === 'connecting'">
      @if (!relay.isSupported()) {
        <p class="usb-phone-support" role="status">
          WebUSB is unavailable here. Use desktop Chromium over HTTPS or localhost.
        </p>
      }

      @if (relay.state().status === 'connected') {
        <p class="usb-phone-status" role="status">
          Phone connected from this browser:
          <code>{{ relay.state().serial }}</code>. Keep this tab open while using the phone.
        </p>
      } @else {
        <button
          type="button"
          class="usb-phone-connect-button"
          [disabled]="relay.state().status === 'connecting'"
          (click)="connect()"
        >
          {{ relay.state().status === 'connecting' ? 'Connecting…' : 'Connect phone from this browser' }}
        </button>
      }

      @if (relay.state().status === 'connecting') {
        <p class="usb-phone-status" role="status">Choose the phone in the browser permission prompt. Keep this tab open.</p>
      }

      @if (relay.state().error; as error) {
        @let errorLines = error.split('\n');
        <p class="usb-phone-error" role="alert">
          {{ errorLines[0] }}
          @if (errorLines.length > 1) {
            <small>{{ errorLines.slice(1).join('\n') }}</small>
          }
        </p>
      }
    </section>
  `,
  styles: [`
    :host {
      display: block;
    }

    .usb-phone-connect {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 12px;
    }

    .usb-phone-connect-button {
      min-height: 38px;
      padding: 0 16px;
      border: 0;
      border-radius: 8px;
      background: var(--color-primary);
      color: var(--color-on-primary);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }

    .usb-phone-connect-button:disabled {
      cursor: wait;
      opacity: 0.65;
    }

    .usb-phone-status,
    .usb-phone-support,
    .usb-phone-error {
      margin: 0;
      font-size: 12px;
      line-height: 1.5;
    }

    .usb-phone-support {
      color: var(--color-text-muted);
    }

    .usb-phone-status {
      color: var(--color-success);
    }

    .usb-phone-status code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-weight: 600;
    }

    .usb-phone-error {
      color: var(--color-error);
    }

    .usb-phone-error small {
      display: block;
      margin-top: 4px;
      font-size: 11px;
      font-weight: normal;
      white-space: pre-line;
    }
  `],
  changeDetection: ChangeDetectionStrategy.Eager
})
export class UsbPhoneConnectionComponent {
  public readonly relay = inject(UsbDeviceRelayService);

  public connect(): void {
    void this.relay.connect();
  }
}
