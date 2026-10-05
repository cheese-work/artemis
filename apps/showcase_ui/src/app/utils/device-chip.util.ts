import { Computer, RegistryDevice } from '../core/models/host.model';
import { COMPUTER_STRINGS, offlineText } from './computer-strings';

export type ChipState = 'Ready' | 'Offline' | 'Needs attention';

export interface DeviceChipView {
  state: ChipState;
  /** Where the phone is plugged in: "This browser" or the computer's name. */
  source: string;
  label: string;
  detail: string;
  /** Material icon name, so state never relies on colour alone. */
  icon: string;
  since: number | null;
}

/**
 * `ownBrowserSerial` is the bridge serial this tab holds. The server lists every browser
 * session, so only that one may be called "This browser".
 */
export function deviceChipView(
  device: RegistryDevice,
  computers: Computer[],
  ownBrowserSerial: string | null = null
): DeviceChipView {
  const label = device.model || device.serial;
  if (device.source === 'browser') {
    const own = ownBrowserSerial !== null && device.serial === ownBrowserSerial;
    return {
      state: 'Ready',
      source: own ? COMPUTER_STRINGS.thisBrowser : COMPUTER_STRINGS.aBrowser,
      label,
      detail: own
        ? 'Keep this browser tab open while using the phone.'
        : 'A phone attached from another browser.',
      icon: 'check_circle',
      since: device.since
    };
  }
  const name = device.computer_name ?? computers.find((c) => c.id === device.computer_id)?.name ?? 'the computer';
  const base = { source: name, label, since: device.since };
  switch (device.computer_status) {
    case 'online':
      return { ...base, state: 'Ready', detail: `Shared by ${name}.`, icon: 'check_circle' };
    case 'update_required':
      return {
        ...base,
        state: 'Needs attention',
        detail: `Update the software on ${name} to use this phone.`,
        icon: 'warning'
      };
    case 'revoked':
      return { ...base, state: 'Offline', detail: `${name} was removed by an administrator.`, icon: 'block' };
    default:
      return {
        ...base,
        state: 'Offline',
        detail: offlineText(name, device.reason),
        icon: 'cloud_off'
      };
  }
}
