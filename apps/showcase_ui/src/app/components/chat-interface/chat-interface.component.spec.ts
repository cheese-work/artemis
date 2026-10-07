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
import { provideRouter } from '@angular/router';
import { EMPTY, of } from 'rxjs';
import { DeviceInfo } from '../../core/models/system.model';
import { Session } from '../../core/models/session.model';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { WEBUSB_DEVICE_MANAGER } from '../../services/usb-device-relay.service';
import { RegistryDevice } from '../../core/models/host.model';
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
  const agentStatus = signal('running');
  let systemService: SystemService;
  let registryDevices: RegistryDevice[];

  beforeEach(async () => {
    sessions.set([]);
    agentStatus.set('running');
    registryDevices = [];
    const agentService = {
      sessions,
      activeTab: signal('tasks'),
      agentStatus,
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
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        {
          provide: HostsService,
          useValue: { list: () => of({ enabled: true, hosts: [], devices: registryDevices }) }
        },
        { provide: WEBUSB_DEVICE_MANAGER, useValue: undefined }
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

  it('never says Unknown device for a loopback address that is no longer listed', () => {
    const text = chipText('127.0.0.1:55555', []);
    expect(text).toContain('Phone via a browser');
    expect(text).not.toContain('Unknown device');
    expect(text).not.toContain('127.0.0.1');
  });

  it('never titles a disconnected wireless phone by its address (R3)', () => {
    for (const address of ['192.168.1.12:5555', '[::1]:39129']) {
      const text = chipText(address, []);
      expect(text).not.toContain(address);
      expect(text).not.toContain('Unknown device');
    }
    expect(chipText('192.168.1.12:5555', [])).toContain('Wireless phone');
  });

  it('keeps the wireless phone address in the tooltip only (R3)', () => {
    systemService.readinessReport.set(null);
    sessions.set([session('192.168.1.12:5555')]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
    expect(chip?.getAttribute('title')).toContain('192.168.1.12:5555');
  });

  it('labels a past run by the model recorded with it when the phone is gone', () => {
    systemService.readinessReport.set(null);
    sessions.set([
      {
        ...session('127.0.0.1:55555'),
        status: 'interrupted',
        device_serial: undefined,
        device_info: { device_id: '127.0.0.1:55555', model: 'Pixel 8', device_kind: 'phone' }
      }
    ]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
    const text = (chip?.textContent ?? '').replace(/\s+/g, ' ').trim();
    expect(text).toContain('Pixel 8');
    expect(text).not.toContain('Unknown device');
    expect(text).not.toContain('127.0.0.1');
    expect(chip?.getAttribute('title')).toContain('127.0.0.1:55555');
  });

  it('labels a past run by the registry model when only the registry knows the phone', () => {
    registryDevices = [
      {
        serial: '127.0.0.1:55555',
        model: 'Pixel 6 Pro',
        device_kind: 'phone',
        source: 'browser',
        computer_id: null,
        computer_name: null,
        computer_status: 'online',
        reason: null,
        since: 1
      }
    ];
    const text = chipText('127.0.0.1:55555', []);
    expect(text).toContain('Pixel 6 Pro');
    expect(text).not.toContain('Unknown device');
  });

  it('keeps the raw serial in the tooltip detail', () => {
    systemService.readinessReport.set(null);
    sessions.set([session('127.0.0.1:36411')]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
    expect(chip?.getAttribute('title')).toContain('127.0.0.1:36411');
  });

  it('names the computer that shares the phone as the chip source', () => {
    registryDevices = [
      {
        serial: 'R5CT1',
        model: 'Pixel 8',
        source: 'computer',
        computer_id: 'h1',
        computer_name: 'Lab Mac',
        computer_status: 'online',
        reason: null,
        since: 1
      }
    ];
    const text = chipText('R5CT1', [device({ serial: 'R5CT1', model: 'Pixel 8' })]);
    expect(text).toContain('Pixel 8');
    expect(text).toContain('Lab Mac');
  });

  it('shows the images sent with a task under its goal, each with alt text', () => {
    const withImages: Session = {
      ...session('emulator-5554'),
      goal_images: [
        { index: 0, media_type: 'image/png', url: '/api/sessions/s1/goal-images/0' },
        { index: 1, media_type: 'image/jpeg', url: '/api/sessions/s1/goal-images/1' }
      ]
    };
    sessions.set([withImages]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();

    const images = (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLImageElement>('.task-goal-images img');
    expect(images.length).toBe(2);
    expect(images[0].getAttribute('src')).toBe('/api/sessions/s1/goal-images/0');
    expect(images[1].alt).toBe('Image 2 sent with this task');
  });

  it('shows no image strip for a task without images', () => {
    sessions.set([session('emulator-5554')]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).querySelector('.task-goal-images')).toBeNull();
  });

  describe('run status badges', () => {
    const rows: Array<[string | undefined, string, 'queue' | 'history']> = [
      ['running', 'Running', 'queue'],
      ['pending', 'Queued', 'queue'],
      ['paused', 'Paused', 'queue'],
      ['completed', 'Passed', 'history'],
      ['failed', 'Failed', 'history'],
      ['cancelled', 'Cancelled', 'history'],
      ['interrupted', 'Interrupted', 'history'],
      ['something_new', 'Unknown', 'history'],
      [undefined, 'Unknown', 'history']
    ];

    for (const live of ['running', 'paused']) {
      it(`shows an unrecognised stored status as Unknown for the live session while ${live} (R1)`, () => {
        agentStatus.set(live);
        sessions.set([{ ...session('emulator-5554'), session_id: 's1', status: 'something_new' }]);
        const fixture = TestBed.createComponent(ChatInterfaceComponent);
        fixture.detectChanges();
        const badge = (fixture.nativeElement as HTMLElement).querySelector('.task-badge');
        expect(badge?.textContent?.trim()).toBe('Unknown');
      });
    }

    for (const [status, label, where] of rows) {
      it(`shows ${status ?? 'a missing status'} as ${label} in the ${where}`, () => {
        sessions.set([{ ...session('emulator-5554'), session_id: 'other', status }]);
        const fixture = TestBed.createComponent(ChatInterfaceComponent);
        fixture.detectChanges();
        const root = fixture.nativeElement as HTMLElement;
        const badge = root.querySelector('.task-badge');
        expect(badge?.textContent?.trim()).toBe(label);
        const inHistory = root.querySelector('.history-section .task-badge') !== null;
        expect(inHistory).toBe(where === 'history');
      });
    }
  });
});
