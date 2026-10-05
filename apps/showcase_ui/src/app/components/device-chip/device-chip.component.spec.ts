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

import { TestBed } from '@angular/core/testing';
import { RegistryDevice } from '../../core/models/host.model';
import { DeviceChipComponent } from './device-chip.component';

describe('DeviceChipComponent', () => {
  it('shows model/kind as the label and keeps the raw address in the detail line', () => {
    const device: RegistryDevice = {
      serial: '127.0.0.1:40001',
      model: null,
      device_kind: 'unknown',
      source: 'browser',
      computer_id: null,
      computer_name: null,
      computer_status: 'online',
      reason: null,
      since: null
    };
    const fixture = TestBed.createComponent(DeviceChipComponent);
    fixture.componentRef.setInput('device', device);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.label')?.textContent?.trim()).toBe('Unknown device');
    expect(root.querySelector('.label')?.textContent).not.toContain('127.0.0.1');
    expect(root.querySelector('.detail')?.textContent).toContain('127.0.0.1:40001');
  });
});
