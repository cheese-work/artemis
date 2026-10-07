import { Computer, RegistryDevice } from '../core/models/host.model';
import { RunTarget } from '../core/models/run-target.model';
import { DeviceInfo } from '../core/models/system.model';
import { UsbDeviceRelayState } from '../services/usb-device-relay.service';
import { deviceSourceOf } from './device-chip.util';
import { deviceKindLabel, deviceTitle } from './device-label.util';

/** The Workspace chip's states. A phone is only ever "connected" when a run could use it now. */
export type WorkspaceChipKind = 'none' | 'connecting' | 'connected' | 'dropped' | 'interrupted';

export interface WorkspaceChipInput {
  relay: UsbDeviceRelayState;
  webUsbSupported: boolean;
  /** The server-scoped adb list: this person's own phones plus shared devices. */
  devices: DeviceInfo[];
  registry: RegistryDevice[];
  computers: Computer[];
  /** The phone this browser remembered for runs; null when none was picked. */
  selected: string | null;
  /** The run on screen was interrupted by a lost phone. */
  runInterrupted: boolean;
}

export interface WorkspaceChipView {
  kind: WorkspaceChipKind;
  /** Chip text: "No phone", "Pixel 6 · …a1b2 · via this browser", … */
  text: string;
  /** One line under the text in the picker, and the hint a screen reader gets. */
  hint: string;
  icon: string;
  /** What the next run binds to; null unless `kind` is `connected`. */
  target: RunTarget | null;
}

export interface PickerOption {
  serial: string;
  text: string;
  /** False while the phone waits for USB-debugging approval or is offline. */
  usable: boolean;
  note: string | null;
}

/** The last four characters of a serial, so two identical phones can be told apart. */
export function serialSuffix(serial: string): string {
  return `…${serial.slice(-4)}`;
}

function sourceText(input: WorkspaceChipInput, device: DeviceInfo | undefined, serial: string): string | null {
  const ownBrowser = input.relay.status === 'connected' ? input.relay.serial : null;
  const source = deviceSourceOf(serial, input.registry, input.computers, ownBrowser);
  if (source === 'This browser') return 'via this browser';
  if (source) return device?.device_kind === 'emulator' || device?.is_emulator ? `emulator on ${source}` : `via ${source}`;
  return device?.device_kind === 'emulator' || device?.is_emulator ? 'emulator' : null;
}

export function pickerOptions(input: WorkspaceChipInput): PickerOption[] {
  return input.devices.map((device) => ({
    serial: device.serial,
    text: [deviceTitle(device), serialSuffix(device.serial), sourceText(input, device, device.serial)]
      .filter(Boolean)
      .join(' · '),
    usable: device.state === 'device',
    note:
      device.state === 'device'
        ? device.is_locked
          ? 'Unlock the phone to run.'
          : null
        : device.state === 'unauthorized'
          ? 'Unlock the phone and tap Allow.'
          : 'The phone is offline.'
  }));
}

export function workspaceChipView(input: WorkspaceChipInput): WorkspaceChipView {
  const { relay } = input;
  const view = (kind: WorkspaceChipKind, text: string, hint: string, icon: string, target: RunTarget | null = null) =>
    ({ kind, text, hint, icon, target }) satisfies WorkspaceChipView;

  if (relay.status === 'connecting') {
    return view('connecting', 'Connecting…', 'Choose the phone, then unlock it and tap Allow.', 'progress_activity');
  }

  // A remembered phone counts only while it is listed and ready. The phone this tab holds
  // counts as soon as the bridge says it is attached; the adb list may lag a few seconds.
  const own = relay.status === 'connected' && relay.serial ? relay.serial : null;
  const listed = input.selected ? input.devices.find((device) => device.serial === input.selected) : undefined;
  const serial = listed?.state === 'device' ? listed.serial : own;
  if (serial) {
    const device = input.devices.find((d) => d.serial === serial);
    const text = [
      device ? deviceTitle(device) : deviceKindLabel({ serial, model: null }),
      serialSuffix(serial),
      sourceText(input, device, serial)
    ]
      .filter(Boolean)
      .join(' · ');
    return view('connected', text, 'Runs start on this phone.', 'smartphone', {
      serial,
      bridgeSessionId: serial === own ? relay.sessionId : null
    });
  }

  if (relay.status === 'dropped') {
    return view('dropped', 'Phone disconnected', 'Reconnect the phone to run.', 'phonelink_erase');
  }
  if (input.runInterrupted) {
    return view('interrupted', 'Run interrupted', 'Reconnect the phone. Reconnecting does not resume the run.', 'warning');
  }
  if (listed?.state === 'unauthorized') {
    return view('none', 'Allow USB debugging', 'Unlock the phone and tap Allow.', 'lock');
  }
  return view(
    'none',
    'No phone · Connect',
    input.webUsbSupported ? 'Connect a phone first.' : 'Use Chrome on a computer to connect a phone.',
    'smartphone'
  );
}
