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

import { deviceKindLabel, deviceSource, deviceTitle } from './device-label.util';

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

  it('names the source only when this browser relays the device', () => {
    expect(deviceSource(browserPhone, '127.0.0.1:36411')).toBe('This browser');
    expect(deviceSource(browserPhone, null)).toBeNull();
    expect(deviceSource(browserPhone, 'other')).toBeNull();
  });
});
