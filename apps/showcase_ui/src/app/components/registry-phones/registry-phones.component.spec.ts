import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { HttpErrorResponse } from '@angular/common/http';
import { Computer, HostsResponse, RegistryDevice } from '../../core/models/host.model';
import { HostsService } from '../../services/hosts.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { RegistryPhonesComponent } from './registry-phones.component';

const computer: Computer = {
  id: 'h1',
  name: 'Lab Mac',
  os: 'macOS 15',
  agent_version: '0.1.0',
  protocol_version: 1,
  status: 'online',
  reason: null,
  since: 1_800_000_000,
  phones_shared: 1,
  phones_not_shared: 0,
  unshared_serials: [],
  share_command: 'smartqa-host share <serial>',
  active_run_count: 0
};

const sharedPhone: RegistryDevice = {
  serial: 'R5CT1',
  model: 'Pixel 8',
  source: 'computer',
  computer_id: 'h1',
  computer_name: 'Lab Mac',
  computer_status: 'online',
  reason: null,
  since: 1_800_000_000
};

const browserPhone: RegistryDevice = {
  serial: '127.0.0.1:5000',
  model: null,
  source: 'browser',
  computer_id: null,
  computer_name: null,
  computer_status: 'online',
  reason: null,
  since: null
};

describe('RegistryPhonesComponent', () => {
  let hosts: jasmine.SpyObj<HostsService>;
  let fixture: ComponentFixture<RegistryPhonesComponent>;

  function create(response: HostsResponse | Error): void {
    hosts.list.and.returnValue(response instanceof Error ? throwError(() => response) : of(response));
    fixture = TestBed.createComponent(RegistryPhonesComponent);
    fixture.detectChanges();
  }

  const root = () => fixture.nativeElement as HTMLElement;

  beforeEach(async () => {
    hosts = jasmine.createSpyObj<HostsService>('HostsService', ['list']);
    await TestBed.configureTestingModule({
      imports: [RegistryPhonesComponent],
      providers: [
        { provide: HostsService, useValue: hosts },
        {
          provide: UsbDeviceRelayService,
          useValue: { state: signal({ status: 'connected', serial: '127.0.0.1:5000', error: null }) }
        }
      ]
    }).compileComponents();
  });

  it('lists each phone as a chip with the computer name or This browser as its source', () => {
    create({ enabled: true, hosts: [computer], devices: [sharedPhone, browserPhone] });
    const chips = root().querySelectorAll('app-device-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toContain('Pixel 8');
    expect(chips[0].textContent).toContain('Lab Mac');
    expect(chips[1].textContent).toContain('This browser');
  });

  it('does not call another browser\'s phone "This browser"', () => {
    create({
      enabled: true,
      hosts: [],
      devices: [browserPhone, { ...browserPhone, serial: '127.0.0.1:6000' }]
    });
    const chips = root().querySelectorAll('app-device-chip');
    expect(chips[0].textContent).toContain('This browser');
    expect(chips[1].textContent).toContain('A browser');
    expect(chips[1].textContent).not.toContain('This browser');
    expect(chips[1].textContent).not.toContain('Keep this browser tab open');
  });

  it('shows an offline computer\'s phone as offline with the reason, not as selectable', () => {
    create({
      enabled: true,
      hosts: [{ ...computer, status: 'offline', reason: 'timeout' }],
      devices: [{ ...sharedPhone, computer_status: 'offline', reason: 'timeout' }]
    });
    expect(root().textContent).toContain('Offline');
    expect(root().textContent).toContain('Lab Mac lost its connection.');
    expect(root().querySelector('button')).toBeNull();
  });

  it('renders nothing when computers are off, empty, or the request fails', () => {
    create({ enabled: false, hosts: [], devices: [] });
    expect(root().querySelector('section')).toBeNull();
    fixture.destroy();
    create({ enabled: true, hosts: [], devices: [] });
    expect(root().querySelector('section')).toBeNull();
    fixture.destroy();
    create(new HttpErrorResponse({ status: 500 }));
    expect(root().querySelector('section')).toBeNull();
  });

  it('never uses the banned word', () => {
    create({ enabled: true, hosts: [computer], devices: [sharedPhone] });
    expect(root().textContent!.toLowerCase()).not.toContain('daemon');
  });
});
