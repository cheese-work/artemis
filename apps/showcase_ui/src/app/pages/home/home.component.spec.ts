import { provideHttpClient, withXhr } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { CUSTOM_ELEMENTS_SCHEMA } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { EMPTY, of } from 'rxjs';
import { RegistryPhonesComponent } from '../../components/registry-phones/registry-phones.component';
import { UsbPhoneConnectionComponent } from '../../components/usb-phone-connection/usb-phone-connection.component';
import { RegistryDevice } from '../../core/models/host.model';
import { DeviceInfo, SystemReadinessReport } from '../../core/models/system.model';
import { AdminConfigService } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
import { SystemService } from '../../services/system.service';
import { WEBUSB_DEVICE_MANAGER } from '../../services/usb-device-relay.service';
import { SetupComponent } from '../setup/setup.component';
import { HomeComponent } from './home.component';

describe('HomeComponent phone surface', () => {
  afterEach(() => TestBed.inject(SystemService).stopAutoPolling());

  it('lists phones from computers and this browser with their source, next to the phone connection', async () => {
    const device = {
      serial: 'R5CT1',
      model: 'Pixel 8',
      source: 'computer',
      computer_id: 'h1',
      computer_name: 'Lab Mac',
      computer_status: 'online',
      reason: null,
      since: 1_800_000_000
    };
    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        provideHttpClient(withXhr()),
        provideHttpClientTesting(),
        { provide: HostsService, useValue: { list: () => of({ enabled: true, hosts: [], devices: [device] }) } },
        {
          provide: AdminConfigService,
          useValue: {
            getIdentity: () => of({ email: null, admin: false, auth_mode: 'open', reason: null }),
            getConfig: () =>
              of({
                version: 'v1',
                default: { provider: 'openai', model: 'm' },
                providers: [],
                sources: { default: 'x', env: 'y' }
              })
          }
        }
      ]
    }).compileComponents();
    const fixture = TestBed.createComponent(HomeComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const chip = (fixture.nativeElement as HTMLElement).querySelector('app-registry-phones app-device-chip');
    expect(chip).not.toBeNull();
    expect(chip!.textContent).toContain('Pixel 8');
    expect(chip!.textContent).toContain('Lab Mac');
  });
});

function device(overrides: Partial<DeviceInfo>): DeviceInfo {
  return {
    serial: '127.0.0.1:36411',
    state: 'device',
    model: '21081111RG',
    product: null,
    android_version: '12',
    screen_resolution: '1080x2400',
    is_locked: false,
    is_emulator: false,
    device_kind: 'phone',
    ...overrides
  };
}

function report(devices: DeviceInfo[]): SystemReadinessReport {
  return {
    overall_ready: true,
    blocker_count: 0,
    passed_blocker_count: 0,
    probes: [
      {
        id: 'android_adb',
        category: 'device',
        title: 'Device',
        status: 'pass',
        is_blocker: true,
        summary: 'Connected',
        description: '',
        metadata: { installed: true, devices },
        actions: []
      }
    ],
    active_device: devices[0] ?? null,
    timestamp: 1
  } as SystemReadinessReport;
}

describe('HomeComponent device card', () => {
  let systemService: SystemService;
  let registryDevices: RegistryDevice[];

  afterEach(() => systemService.stopAutoPolling());

  beforeEach(async () => {
    registryDevices = [];
    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        provideHttpClient(withXhr()),
        provideHttpClientTesting(),
        {
          provide: HostsService,
          useValue: { list: () => of({ enabled: true, hosts: [], devices: registryDevices }) }
        },
        { provide: WEBUSB_DEVICE_MANAGER, useValue: undefined }
      ]
    })
      .overrideComponent(HomeComponent, {
        remove: { imports: [UsbPhoneConnectionComponent, RegistryPhonesComponent, SetupComponent] },
        add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
      })
      .compileComponents();
    systemService = TestBed.inject(SystemService);
    spyOn(systemService, 'fetchReadiness').and.returnValue(EMPTY);
    spyOn(systemService, 'fetchCredentialStatus').and.returnValue(EMPTY);
    spyOn(systemService, 'fetchAdbServerStatus').and.returnValue(EMPTY);
  });

  function render(devices: DeviceInfo[]): HTMLElement {
    systemService.readinessReport.set(report(devices));
    const fixture = TestBed.createComponent(HomeComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function text(root: HTMLElement, selector: string): string {
    return (root.querySelector(selector)?.textContent ?? '').replace(/\s+/g, ' ').trim();
  }

  it('shows a browser-relayed phone as a phone with its model name', () => {
    const root = render([device({})]);
    expect(text(root, '.device-hero-name')).toBe('21081111RG');
    const meta = text(root, '.device-hero-meta');
    expect(meta).toContain('Phone');
    expect(meta).toContain('127.0.0.1:36411');
    expect(meta).not.toContain('Virtual Device');
    expect(text(root, '.device-hero-name')).not.toContain('127.0.0.1');
  });

  it('shows a real emulator as an emulator', () => {
    const root = render([
      device({ serial: 'emulator-5554', model: 'sdk_gphone64_arm64', is_emulator: true, device_kind: 'emulator' })
    ]);
    expect(text(root, '.device-hero-meta')).toContain('Emulator');
  });

  it('shows an unclassifiable device as Unknown device, not Virtual Device', () => {
    const root = render([device({ model: null, device_kind: 'unknown' })]);
    expect(text(root, '.device-hero-name')).toBe('Unknown device');
    expect(text(root, '.device-hero-meta')).not.toContain('Virtual Device');
    expect(text(root, '.device-hero-name')).not.toContain('127.0.0.1');
  });

  it('labels each picker chip by model name and kind', () => {
    systemService.configWritesLocked.set(false);
    const root = render([
      device({}),
      device({ serial: 'emulator-5554', model: 'sdk_gphone64_arm64', is_emulator: true, device_kind: 'emulator' }),
      device({ serial: '127.0.0.1:40001', model: null, device_kind: 'unknown' })
    ]);
    const chips = Array.from(root.querySelectorAll('.device-chip-btn')).map(c =>
      (c.textContent ?? '').replace(/\s+/g, ' ').trim()
    );
    expect(chips.length).toBe(3);
    expect(chips[0]).toContain('21081111RG');
    expect(chips[0]).toContain('Phone');
    expect(chips[1]).toContain('Emulator');
    expect(chips[2]).toContain('Unknown device');
    expect(chips.join('|')).not.toContain('127.0.0.1');
  });

  it('names the computer that shares a phone as its source in the card and the picker', () => {
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
    systemService.configWritesLocked.set(false);
    const root = render([
      device({ serial: 'R5CT1', model: 'Pixel 8' }),
      device({ serial: 'emulator-5554', model: 'sdk_gphone64_arm64', is_emulator: true, device_kind: 'emulator' })
    ]);
    expect(text(root, '.device-hero-meta')).toContain('Lab Mac');
    const chips = Array.from(root.querySelectorAll('.device-chip-btn')).map(c =>
      (c.textContent ?? '').replace(/\s+/g, ' ').trim()
    );
    expect(chips[0]).toContain('Lab Mac');
    expect(chips[1]).not.toContain('Lab Mac');
  });
});
