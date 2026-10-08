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

import { deviceKindLabel, deviceTitle, serialShape, unlistedDeviceTitle, unlistedRunDeviceTitle } from './device-label.util';

describe('device-label.util', () => {
  const browserPhone = { serial: '127.0.0.1:36411', model: '21081111RG', device_kind: 'phone' as const };

  it('labels a loopback-serial phone as a phone with its model name', () => {
    expect(deviceKindLabel(browserPhone)).toBe('Phone');
    expect(deviceTitle(browserPhone)).toBe('21081111RG');
  });

  it('labels an emulator as an emulator', () => {
    const emulator = { serial: 'emulator-5554', model: 'sdk_gphone64_arm64', device_kind: 'emulator' as const };
    expect(deviceKindLabel(emulator)).toBe('Emulator');
    expect(deviceTitle(emulator)).toBe('sdk_gphone64_arm64');
  });

  it('never calls an unclassified device a virtual device or titles it by its address', () => {
    const unknown = { serial: '127.0.0.1:40001', model: null, device_kind: 'unknown' as const };
    expect(deviceKindLabel(unknown)).toBe('Unknown device');
    expect(deviceTitle(unknown)).toBe('Unknown device');
    expect(deviceKindLabel({ serial: 'x', model: null })).toBe('Unknown device');
  });

  it('titles a run whose phone is gone by where it was attached, never as Unknown device', () => {
    expect(unlistedRunDeviceTitle('127.0.0.1:55555', true)).toBe('Phone via this browser');
    expect(unlistedRunDeviceTitle('127.0.0.1:55555', false)).toBe('Phone via a browser');
    expect(unlistedRunDeviceTitle('localhost:5555', false)).toBe('Phone via a browser');
    expect(unlistedRunDeviceTitle('[::1]:39129', false)).toBe('Phone via a browser');
    expect(unlistedRunDeviceTitle('emulator-5554', false)).toBe('emulator-5554');
    expect(unlistedRunDeviceTitle('R58M123', false)).toBe('R58M123');
  });

  it('never titles a disconnected network phone by its address (R3)', () => {
    for (const address of ['192.168.1.12:5555', '10.0.0.7:37099', 'pixel.local:5555', '[fe80::1]:5555', 'pixel:5555', 'android-phone:37099', 'Pixel_6:5555']) {
      const title = unlistedRunDeviceTitle(address, false);
      expect(title).toBe('Wireless phone');
      expect(title).not.toContain(address);
    }
  });

  it('treats mDNS wireless-debugging names as network phones (R3)', () => {
    const name = 'adb-R58M123ABC-xYz9Qk._adb-tls-connect._tcp';
    expect(unlistedRunDeviceTitle(name, false)).toBe('Wireless phone');
    expect(serialShape(name)).toBe('network');
  });

  it('classifies serials by shape without touching plain serials (R3)', () => {
    expect(serialShape('127.0.0.1:5555')).toBe('loopback');
    expect(serialShape('[::1]:5555')).toBe('loopback');
    expect(serialShape('pixel:5555')).toBe('network');
    expect(serialShape('R58M123')).toBe('plain');
    expect(serialShape('emulator-5554')).toBe('plain');
    expect(serialShape('')).toBe('plain');
  });

  it('keeps addresses out of the title of a phone missing from the registry too (R3)', () => {
    expect(unlistedDeviceTitle('127.0.0.1:55555')).toBe('Unknown device');
    expect(unlistedDeviceTitle('pixel:5555')).toBe('Wireless phone');
    expect(unlistedDeviceTitle('192.168.1.12:5555')).toBe('Wireless phone');
    expect(unlistedDeviceTitle('R58M123')).toBe('R58M123');
  });
});
