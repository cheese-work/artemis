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

import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { EMPTY } from 'rxjs';
import { DeviceInfo } from '../../core/models/system.model';
import { Session } from '../../core/models/session.model';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { ChatInterfaceComponent } from './chat-interface.component';

function session(serial: string): Session {
  return { session_id: 's1', initial_goal: 'goal', start_time: 1, status: 'running', device_serial: serial };
}

function device(overrides: Partial<DeviceInfo>): DeviceInfo {
  return {
    serial: '127.0.0.1:36411',
    state: 'device',
    model: '21081111RG',
    product: null,
    android_version: '12',
    screen_resolution: null,
    is_locked: false,
    is_emulator: false,
    device_kind: 'phone',
    ...overrides
  };
}

describe('ChatInterfaceComponent device chip', () => {
  const sessions = signal<Session[]>([]);
  let systemService: SystemService;

  beforeEach(async () => {
    sessions.set([]);
    const agentService = {
      sessions,
      activeTab: signal('tasks'),
      agentStatus: signal('running'),
      runningSessionId: signal('s1'),
      currentSessionId: signal('s1'),
      currentNotes: signal([]),
      selectedNoteKey: signal(null),
      fetchStatus: () => {}
    };
    await TestBed.configureTestingModule({
      imports: [ChatInterfaceComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: AgentService, useValue: agentService }
      ]
    }).compileComponents();
    systemService = TestBed.inject(SystemService);
    spyOn(systemService, 'fetchReadiness').and.returnValue(EMPTY);
  });

  function chipText(serial: string, devices: DeviceInfo[]): string {
    systemService.readinessReport.set({
      probes: [
        {
          id: 'android_adb',
          category: 'device',
          title: 'Device',
          status: 'pass',
          is_blocker: true,
          summary: 'Connected',
          description: '',
          metadata: { devices },
          actions: []
        }
      ]
    } as never);
    sessions.set([session(serial)]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
    return (chip?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  it('labels a browser-relayed phone by model and kind, not by its address', () => {
    const text = chipText('127.0.0.1:36411', [device({})]);
    expect(text).toContain('21081111RG');
    expect(text).toContain('Phone');
    expect(text).not.toContain('127.0.0.1');
  });

  it('labels a real emulator as an emulator', () => {
    const text = chipText('emulator-5554', [
      device({ serial: 'emulator-5554', model: 'sdk_gphone64_arm64', is_emulator: true, device_kind: 'emulator' })
    ]);
    expect(text).toContain('Emulator');
  });

  it('shows Unknown device for a loopback address that is no longer listed', () => {
    const text = chipText('127.0.0.1:55555', []);
    expect(text).toContain('Unknown device');
    expect(text).not.toContain('127.0.0.1');
  });

  it('keeps the raw serial in the tooltip detail', () => {
    systemService.readinessReport.set(null);
    sessions.set([session('127.0.0.1:36411')]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
    expect(chip?.getAttribute('title')).toContain('127.0.0.1:36411');
  });
});
