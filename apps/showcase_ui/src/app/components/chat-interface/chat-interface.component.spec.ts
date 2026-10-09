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
import { provideRouter, Router } from '@angular/router';
import { EMPTY, defer, of } from 'rxjs';
import { DeviceInfo } from '../../core/models/system.model';
import { Session } from '../../core/models/session.model';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { WEBUSB_DEVICE_MANAGER } from '../../services/usb-device-relay.service';
import { RegistryDevice } from '../../core/models/host.model';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { SystemService } from '../../services/system.service';
import { RunsService } from '../../services/runs.service';
import { sessionStatusView } from '../../utils/run-status.util';
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
        { provide: RunsService, useValue: {
          lastLibraryQuery: signal({}),
          list: () => defer(() => of({
            runs: sessions().filter((session) => !sessionStatusView(session.status, null).active).map((session) => {
              const info = typeof session.device_info === 'string' ? JSON.parse(session.device_info) : session.device_info;
              return {
                session_id: session.session_id, prompt: session.initial_goal, status: session.status,
                start_time: session.start_time, end_time: null, host_id: null,
                device_ref: { host_id: null, serial: session.device_serial ?? session.device_id ?? info?.device_id ?? null },
                requested_by: null, interrupt_reason: null, pinned: false, recordings: []
              };
            }),
            next_cursor: null, warnings: []
          }))
        } },
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
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device, .run-device');
    return (chip?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  it('characterizes the past-run list separately from the active queue', () => {
    sessions.set([
      { ...session('emulator-5554'), session_id: 'past', status: 'failed', initial_goal: 'Past prompt' },
      { ...session('emulator-5554'), session_id: 'live', status: 'running', initial_goal: 'Live prompt' }
    ]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const history = (fixture.nativeElement as HTMLElement).querySelector('.date-group');
    expect(history?.textContent).toContain('Past prompt');
    expect(history?.textContent).toContain('Failed');
    expect(history?.textContent).not.toContain('Live prompt');
    const queue = (fixture.nativeElement as HTMLElement).querySelector('.queue-section');
    expect(queue?.textContent).toContain('Live prompt');
    expect(queue?.textContent).not.toContain('Past prompt');
  });

  it('uses the short title in queue rows and their Stop controls', () => {
    sessions.set([{ ...session('emulator-5554'), initial_goal: '\n## **Open** _Settings_\nCheck every toggle.' }]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.task-goal')?.textContent?.trim()).toBe('Open Settings');
    expect(root.querySelector('.queue-stop')?.getAttribute('title')).toBe('Stop run: Open Settings');
    expect(root.querySelector('.queue-stop')?.getAttribute('aria-label')).toBe('Stop run: Open Settings');
  });

  it('renders the list-owned empty state without an outline', () => {
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const emptyList = root.querySelector<HTMLElement>('app-run-library .state-empty');
    expect(root.querySelector('.empty-section-placeholder')).toBeNull();
    expect(emptyList).not.toBeNull();
    expect(getComputedStyle(emptyList!).borderStyle).toBe('none');
  });

  it('refreshes history when a run completes after the first catalog load', () => {
    sessions.set([session('emulator-5554')]);
    const runs = TestBed.inject(RunsService);
    const list = spyOn(runs, 'list').and.callThrough();
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.date-group .run-row')).toBeNull();
    expect(list).toHaveBeenCalledTimes(1);

    agentStatus.set('idle');
    sessions.set([{ ...session('emulator-5554'), status: 'completed', end_time: 2 }]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.date-group .run-row')?.textContent).toContain('goal');
    expect(list).toHaveBeenCalledTimes(2);
    expect(fixture.nativeElement.querySelector('.queue-section')).toBeNull();

    sessions.set([{ ...session('emulator-5554'), status: 'completed', end_time: 2 }]);
    fixture.detectChanges();
    expect(list).toHaveBeenCalledTimes(2);
  });

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

  function chipTitle(serial: string): string {
    systemService.readinessReport.set(null);
    sessions.set([session(serial)]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    return (fixture.nativeElement as HTMLElement).querySelector('.task-device .device-name')?.textContent?.trim() ?? '';
  }

  it('uses tabular mono for serials and times with a 44 px queue action', () => {
    systemService.readinessReport.set(null);
    sessions.set([session('emulator-5554')]);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    for (const selector of ['.device-name', '.run-date']) {
      const style = getComputedStyle(root.querySelector(selector)!);
      expect(style.fontFamily).toContain('JetBrains Mono');
      expect(style.fontVariantNumeric).toBe('tabular-nums');
    }
    const box = root.querySelector('.queue-stop')!.getBoundingClientRect();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
  });

  it('never titles a disconnected wireless phone by its address (R3)', () => {
    for (const address of ['192.168.1.12:5555', '[::1]:39129', 'pixel:5555', 'android-phone:37099']) {
      const text = chipText(address, []);
      expect(text).not.toContain(address);
      expect(text).not.toContain('Unknown device');
    }
    for (const address of ['192.168.1.12:5555', 'pixel:5555', 'android-phone:37099']) {
      expect(chipText(address, [])).toContain('Wireless phone');
      expect(chipTitle(address)).toBe('Wireless phone');
    }
  });

  it('keeps the wireless phone address in the tooltip only (R3)', () => {
    systemService.readinessReport.set(null);
    for (const address of ['192.168.1.12:5555', 'pixel:5555', 'android-phone:37099']) {
      sessions.set([session(address)]);
      const fixture = TestBed.createComponent(ChatInterfaceComponent);
      fixture.detectChanges();
      const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device');
      expect(chip?.getAttribute('title')).toContain(address);
    }
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
    const chip = (fixture.nativeElement as HTMLElement).querySelector('.task-device, .run-device');
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
    expect(text).not.toContain('A browser');
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

  for (const prefix of ['/', '/preview/pr/70/']) {
    for (const status of ['running', 'completed']) {
      it(`scopes ${status} task goal images under ${prefix} and keeps their alt text`, () => {
        spyOnProperty(document, 'baseURI', 'get').and.returnValue(new URL(prefix, location.href).href);
        const withImages: Session = {
          ...session('emulator-5554'),
          status,
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
        expect(images[0].getAttribute('src')).toBe(`${prefix}api/sessions/s1/goal-images/0`);
        expect(images[1].getAttribute('src')).toBe(`${prefix}api/sessions/s1/goal-images/1`);
        expect(images[1].alt).toBe('Image 2 sent with this task');
      });
    }
  }

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
      ['completed', 'Completed', 'history'],
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
        const badge = (fixture.nativeElement as HTMLElement).querySelector('.task-badge, .run-outcome');
        expect(badge?.textContent).toContain('Unknown');
      });
    }

    for (const [status, label, where] of rows) {
      it(`shows ${status ?? 'a missing status'} as ${label} in the ${where}`, () => {
        sessions.set([{ ...session('emulator-5554'), session_id: 'other', status }]);
        const fixture = TestBed.createComponent(ChatInterfaceComponent);
        fixture.detectChanges();
        const root = fixture.nativeElement as HTMLElement;
        const badge = root.querySelector('.task-badge, .run-outcome');
        expect(badge?.textContent).toContain(label);
        const inHistory = root.querySelector('.date-group .run-outcome') !== null;
        expect(inHistory).toBe(where === 'history');
      });
    }
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
        { provide: RunsService, useValue: {
          lastLibraryQuery: signal({}),
          list: () => of({ runs: [{
            session_id: 'h1', prompt: 'Past prompt', status: 'completed', requested_by: null,
            start_time: 1, end_time: null, host_id: null, device_ref: null, pinned: false,
            recordings: [], read_only: true
          }], next_cursor: null, warnings: [] })
        } },
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
    expect(owners()).toEqual(['Owner: qa1@example.test']);
  });

  it('labels shared history with the owner independently of the admin queue scope', async () => {
    scope.identity.set({ email: 'admin@example.test', admin: true, auth_mode: 'cloudflare', reason: null });
    scope.setAllUsers(true);
    await TestBed.inject(Router).navigateByUrl('/?scope=everyone');
    const { fixture, owners } = render();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.date-group .run-owner')!.textContent).toContain('No owner');
    expect(root.querySelector('.task-card .owner-label')!.textContent).toContain('qa1@example.test');
    expect(owners()).toEqual(['Owner: qa1@example.test']);
    expect(root.querySelector('.date-group .run-read-only')).toBeNull();
    expect(root.querySelector('.date-group .run-row')!.getAttribute('href')).toContain('review=1');
  });
});
