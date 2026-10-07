import { signal } from '@angular/core';
import { of } from 'rxjs';
import { DeviceInfo } from '../core/models/system.model';
import { AgentService } from '../services/agent.service';
import { HostsService } from '../services/hosts.service';
import { SystemService } from '../services/system.service';
import { UsbDeviceRelayService, UsbDeviceRelayState } from '../services/usb-device-relay.service';

export const IDLE: UsbDeviceRelayState = { status: 'idle', serial: null, sessionId: null, error: null };

export const phone = (over: Partial<DeviceInfo> = {}): DeviceInfo => ({
  serial: 'R58M1234a1b2',
  state: 'device',
  model: 'Pixel 6',
  product: null,
  android_version: null,
  screen_resolution: null,
  is_locked: false,
  is_emulator: false,
  device_kind: 'phone',
  ...over
});

/** Stand-ins for everything the Workspace phone service reads, so a spec drives it by setting signals. */
export function phoneFakes() {
  const relay = {
    state: signal<UsbDeviceRelayState>(IDLE),
    isSupported: signal(true),
    connect: jasmine.createSpy('connect').and.resolveTo(undefined),
    disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
  };
  const selected = signal<string | null>(null);
  const system = {
    connectedDevices: signal<DeviceInfo[]>([]),
    selectedRunTarget: selected,
    chooseRunTarget: jasmine.createSpy('chooseRunTarget').and.callFake((serial: string | null) => selected.set(serial)),
    // Admin-only server actions: no QA-facing phone control may ever call these.
    restartAdb: jasmine.createSpy('restartAdb'),
    selectDevice: jasmine.createSpy('selectDevice'),
    useLocalAdbServer: jasmine.createSpy('useLocalAdbServer')
  };
  const agent = {
    isCurrentSessionRunning: signal(false),
    currentSession: signal<{ status?: string; initial_goal?: string; interrupt_reason?: string | null } | null>(null),
    currentSessionStepFrames: signal<{ stepNumber: number }[]>([]),
    resumeTask: jasmine.createSpy('resumeTask')
  };
  const hosts = { list: jasmine.createSpy('list').and.returnValue(of({ enabled: true, hosts: [], devices: [] })) };
  const providers = [
    { provide: UsbDeviceRelayService, useValue: relay },
    { provide: SystemService, useValue: system },
    { provide: AgentService, useValue: agent },
    { provide: HostsService, useValue: hosts }
  ];
  return { relay, system, agent, hosts, providers };
}
