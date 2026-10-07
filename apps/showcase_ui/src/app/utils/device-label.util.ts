/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { DeviceInfo, DeviceKind } from '../core/models/system.model';

export type LabelableDevice = Pick<DeviceInfo, 'serial' | 'model'> & { device_kind?: DeviceKind };

const KIND_LABELS: Record<DeviceKind, string> = {
  phone: 'Phone',
  emulator: 'Emulator',
  unknown: 'Unknown device'
};

/** What the device is, as classified by the server from its adb properties. */
export function deviceKindLabel(device: LabelableDevice): string {
  return KIND_LABELS[device.device_kind ?? 'unknown'];
}

/** Primary label: the real model name; never the serial or address. */
export function deviceTitle(device: LabelableDevice): string {
  return device.model?.trim() || deviceKindLabel(device);
}

const LOOPBACK_ADDRESS = /^(127\.0\.0\.1|localhost):\d+$/;

/**
 * Title for a device that is not (or no longer) in the live device list. An
 * address like 127.0.0.1:<port> says nothing about the device, so it is never
 * shown as a label.
 */
export function unlistedDeviceTitle(serial: string): string {
  return LOOPBACK_ADDRESS.test(serial) ? KIND_LABELS.unknown : serial;
}

/**
 * Title for a past run whose phone is no longer listed. A loopback address is how a browser
 * relays a phone, so say that rather than "Unknown device"; the address stays in a detail line.
 */
export function unlistedRunDeviceTitle(serial: string, ownBrowser: boolean): string {
  if (!LOOPBACK_ADDRESS.test(serial)) {
    return serial;
  }
  return ownBrowser ? 'Phone via this browser' : 'Phone via a browser';
}

/** True when the record says what the device is: a model name or a classified kind. */
export function isIdentifiedDevice(device: LabelableDevice): boolean {
  return !!device.model?.trim() || (!!device.device_kind && device.device_kind !== 'unknown');
}

export function runDeviceLabel(
  serial: string | null,
  device: LabelableDevice | null = null,
  ownBrowser = false,
  detail = false
): string {
  if (detail) return serial ?? 'Unknown phone';
  return device && isIdentifiedDevice(device) ? deviceTitle(device) : unlistedRunDeviceTitle(serial ?? '', ownBrowser);
}
