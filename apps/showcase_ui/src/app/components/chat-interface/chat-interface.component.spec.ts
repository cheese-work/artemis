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
import { OwnerScopeService } from '../../services/owner-scope.service';
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
  let registryDevices: RegistryDevice[];

  beforeEach(async () => {
    sessions.set([]);
    registryDevices = [];
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

  it('lists an interrupted run as interrupted, and a status it does not know as unknown, never completed', () => {
    sessions.set([
      { ...session('a'), session_id: 's2', status: 'interrupted' },
      { ...session('a'), session_id: 's3', status: 'brand_new_status' }
    ]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();

    const badges = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('.task-badge')).map((b) =>
      (b.textContent ?? '').trim()
    );
    expect(badges).toContain('INTERRUPTED');
    expect(badges).toContain('UNKNOWN');
    expect(badges).not.toContain('COMPLETED');
  });
});

describe('ChatInterfaceComponent per-QA scope (CHE-1152)', () => {
  const sessions = signal<Session[]>([]);
  let scope: OwnerScopeService;

  const owned = (id: string, status: string, owner: string | null): Session => ({
    session_id: id,
    initial_goal: `goal ${id}`,
    start_time: 1,
    status,
    requested_by: owner
  });

  function render() {
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    return {
      fixture,
      owners: () => Array.from(root.querySelectorAll('.task-card .owner-label')).map((el) => el.textContent!.trim()),
      switchControl: () => root.querySelector<HTMLButtonElement>('[role="switch"]')
    };
  }

  beforeEach(async () => {
    sessions.set([owned('q1', 'running', 'qa1@example.test'), owned('h1', 'completed', null)]);
    const agentService = {
      sessions,
      activeTab: signal('tasks'),
      agentStatus: signal('running'),
      runningSessionId: signal('q1'),
      currentSessionId: signal('q1'),
      currentNotes: signal([]),
      selectedNoteKey: signal(null),
      fetchStatus: () => {}
    };
    await TestBed.configureTestingModule({
      imports: [ChatInterfaceComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: AgentService, useValue: agentService },
        { provide: HostsService, useValue: { list: () => of({ enabled: true, hosts: [], devices: [] }) } },
        { provide: WEBUSB_DEVICE_MANAGER, useValue: undefined }
      ]
    }).compileComponents();
    spyOn(TestBed.inject(SystemService), 'fetchReadiness').and.returnValue(EMPTY);
    scope = TestBed.inject(OwnerScopeService);
  });

  it('shows a QA neither the All users switch nor any owner label', () => {
    scope.identity.set({ email: 'qa1@example.test', admin: false, auth_mode: 'cloudflare', reason: null });
    const { owners, switchControl } = render();
    expect(switchControl()).toBeNull();
    expect(owners()).toEqual([]);
  });

  it('gives an admin the switch in the Task Queue header, and no labels until it is on', () => {
    scope.identity.set({ email: 'admin@example.test', admin: true, auth_mode: 'cloudflare', reason: null });
    const { switchControl, owners, fixture } = render();
    expect(switchControl()!.closest('.chat-header')).not.toBeNull();
    expect(owners()).toEqual([]);
    scope.setAllUsers(true);
    fixture.detectChanges();
    expect(owners()).toEqual(['Owner: qa1@example.test', 'Owner: No owner']);
  });

  it('labels queue and history rows alike, so a QA\'s run and an unowned run are told apart', () => {
    scope.identity.set({ email: 'admin@example.test', admin: true, auth_mode: 'cloudflare', reason: null });
    scope.setAllUsers(true);
    const { fixture, owners } = render();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.history-section .owner-label')!.textContent).toContain('No owner');
    expect(root.querySelector('.task-card .owner-label')!.textContent).toContain('qa1@example.test');
    expect(owners().length).toBe(2);
  });
});
