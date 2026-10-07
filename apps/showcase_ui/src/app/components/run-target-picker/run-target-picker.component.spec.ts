import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { of } from 'rxjs';
import { DeviceInfo } from '../../core/models/system.model';
import { HostsService } from '../../services/hosts.service';
import { SELECTED_DEVICE_SERIAL_KEY, SystemService } from '../../services/system.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { RunTargetPickerComponent } from './run-target-picker.component';

const device = (over: Partial<DeviceInfo>): DeviceInfo => ({
  serial: 'emulator-5554',
  state: 'device',
  model: 'Pixel 8',
  product: null,
  android_version: null,
  screen_resolution: null,
  is_locked: false,
  is_emulator: false,
  device_kind: 'phone',
  ...over
});

const OWN_NEW = device({ serial: '127.0.0.1:41003', model: 'Pixel 6' });
const OWN_OLD = device({ serial: '127.0.0.1:41001', model: '21081111RG' });
const SHARED = device({ serial: 'emulator-5554', model: 'Pixel 8', device_kind: 'emulator' });

describe('RunTargetPickerComponent', () => {
  let fixture: ComponentFixture<RunTargetPickerComponent>;
  let system: SystemService;
  let http: HttpTestingController;

  // The server already scoped this list to the caller: their own phones plus shared devices.
  function create(devices: DeviceInfo[]): void {
    system.readinessReport.set({
      overall_ready: true,
      blocker_count: 1,
      passed_blocker_count: 1,
      os_type: 'linux',
      timestamp: 1,
      active_device: null,
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
    fixture = TestBed.createComponent(RunTargetPickerComponent);
    fixture.detectChanges();
  }

  const root = () => fixture.nativeElement as HTMLElement;
  const select = () => root().querySelector('select') as HTMLSelectElement;
  const options = () => Array.from(select().options).map((o) => o.textContent!.trim());

  function choose(value: string): void {
    select().value = value;
    select().dispatchEvent(new Event('change'));
    fixture.detectChanges();
  }

  beforeEach(() => {
    localStorage.removeItem(SELECTED_DEVICE_SERIAL_KEY);
    TestBed.configureTestingModule({
      imports: [RunTargetPickerComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: HostsService,
          useValue: {
            list: () =>
              of({
                enabled: true,
                hosts: [],
                devices: [
                  { serial: OWN_NEW.serial, model: 'Pixel 6', source: 'browser', owner: 'qa1@example.com', computer_id: null, computer_name: null, computer_status: 'online', reason: null, since: null },
                  { serial: OWN_OLD.serial, model: '21081111RG', source: 'browser', owner: 'qa1@example.com', computer_id: null, computer_name: null, computer_status: 'online', reason: null, since: null }
                ]
              })
          }
        },
        { provide: UsbDeviceRelayService, useValue: { state: signal({ status: 'idle', serial: null, error: null }) } }
      ]
    });
    system = TestBed.inject(SystemService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => localStorage.removeItem(SELECTED_DEVICE_SERIAL_KEY));

  it('lists Automatic, both of a QA\'s own phones and the shared device with model names first', () => {
    create([OWN_NEW, OWN_OLD, SHARED]);
    expect(options()).toEqual([
      'Automatic',
      'Pixel 6 · Phone · qa1@example.com\'s browser',
      '21081111RG · Phone · qa1@example.com\'s browser',
      'Pixel 8 · Emulator'
    ]);
  });

  it('never shows a bridge address as a label', () => {
    create([OWN_NEW, device({ serial: '127.0.0.1:41009', model: null, device_kind: 'unknown' })]);
    for (const text of options()) {
      expect(text).not.toMatch(/127\.0\.0\.1/);
    }
    expect(options()).toContain('Unknown device');
  });

  it('is labelled and keyboard operable as a native select', () => {
    create([OWN_NEW, SHARED]);
    const label = root().querySelector('label');
    expect(label?.textContent).toContain('Run on');
    expect(label?.getAttribute('for')).toBe(select().id);
  });

  it('uses the light app surface with dark text and at least 4.5:1 contrast', () => {
    create([OWN_NEW, SHARED]);
    const styles = getComputedStyle(select());
    expect(styles.backgroundColor).toBe('rgb(248, 250, 252)');
    expect(styles.color).toBe('rgb(71, 85, 105)');
    expect(styles.borderColor).toBe('rgb(226, 232, 240)');
    const luminance = (channels: number[]) => channels.map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    expect((luminance([248, 250, 252]) + 0.05) / (luminance([71, 85, 105]) + 0.05)).toBeGreaterThanOrEqual(4.5);
    const option = getComputedStyle(select().options[1]);
    expect(option.backgroundColor).toBe(styles.backgroundColor);
    expect(option.color).toBe(styles.color);
    const selected = getComputedStyle(select().options[0]);
    expect(selected.backgroundColor).toBe('rgb(239, 246, 255)');
    expect(selected.color).toBe('rgb(30, 64, 175)');
    expect((luminance([239, 246, 255]) + 0.05) / (luminance([30, 64, 175]) + 0.05)).toBeGreaterThanOrEqual(4.5);
  });

  it('keeps native keyboard focus and commits a selection without moving the shared target', () => {
    create([OWN_NEW, SHARED]);
    select().focus();
    expect(document.activeElement).toBe(select());
    expect(select().tabIndex).toBe(0);
    choose(SHARED.serial);
    expect(system.selectedRunTarget()).toBe(SHARED.serial);
    choose('');
    expect(system.selectedRunTarget()).toBeNull();
    http.expectNone('/api/system/devices/select');
  });

  it('chooses the older of two own phones for the next run, locally, without touching the global target', () => {
    create([OWN_NEW, OWN_OLD, SHARED]);

    choose(OWN_OLD.serial);

    expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe(OWN_OLD.serial);
    expect(system.selectedRunTarget()).toBe(OWN_OLD.serial);
    http.expectNone('/api/system/devices/select');
  });

  it('chooses the shared device instead of the newest own phone', () => {
    create([OWN_NEW, OWN_OLD, SHARED]);

    choose(SHARED.serial);

    expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe(SHARED.serial);
  });

  it('goes back to Automatic by forgetting the choice', () => {
    localStorage.setItem(SELECTED_DEVICE_SERIAL_KEY, OWN_OLD.serial);
    system.selectedRunTarget.set(OWN_OLD.serial);
    create([OWN_NEW, OWN_OLD, SHARED]);
    expect(select().value).toBe(OWN_OLD.serial);

    choose('');

    expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBeNull();
    expect(system.selectedRunTarget()).toBeNull();
  });

  it('shows Automatic when the remembered phone is no longer in the list', () => {
    system.selectedRunTarget.set('127.0.0.1:41999');
    create([OWN_NEW, SHARED]);
    expect(select().value).toBe('');
  });

  it('only offers devices that are ready', () => {
    create([OWN_NEW, device({ serial: 'offline-1', state: 'offline', model: 'Old phone' }), SHARED]);
    expect(options().join('|')).not.toContain('Old phone');
  });

  it('renders nothing when no device is available', () => {
    create([]);
    expect(root().querySelector('select')).toBeNull();
  });
});
