import { signal } from '@angular/core';
import { ComponentFixture, TestBed, discardPeriodicTasks, fakeAsync, flush, flushMicrotasks, tick } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { Computer, HostsResponse, RegistryDevice } from '../../core/models/host.model';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
import { UsbDeviceRelayService } from '../../services/usb-device-relay.service';
import { ComputersComponent } from './computers.component';

const NOW = 1_800_000_000;

const computer = (over: Partial<Computer> = {}): Computer => ({
  id: 'h1',
  name: 'Lab Mac',
  os: 'macOS 15',
  agent_version: '0.1.0',
  protocol_version: 1,
  status: 'online',
  reason: null,
  since: NOW - 120,
  phones_shared: 2,
  phones_not_shared: 1,
  unshared_serials: ['emulator-5554'],
  share_command: 'smartqa-host share <serial>',
  active_run_count: 0,
  ...over
});

const response = (hosts: Computer[], devices: RegistryDevice[] = []): HostsResponse => ({
  enabled: true,
  hosts,
  devices
});

describe('ComputersComponent', () => {
  let hosts: jasmine.SpyObj<HostsService>;
  let adminConfig: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<ComputersComponent>;

  const admin: AdminIdentity = { email: 'a@x.test', admin: true, auth_mode: 'cloudflare', reason: null };
  const member: AdminIdentity = { email: 'q@x.test', admin: false, auth_mode: 'cloudflare', reason: 'not_on_allowlist' };

  beforeEach(async () => {
    hosts = jasmine.createSpyObj<HostsService>('HostsService', [
      'list',
      'createCode',
      'codeStatus',
      'revoke',
      'rename'
    ]);
    adminConfig = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    adminConfig.getIdentity.and.returnValue(of(admin));
    hosts.list.and.returnValue(of(response([computer()])));
    await TestBed.configureTestingModule({
      imports: [ComputersComponent],
      providers: [
        { provide: HostsService, useValue: hosts },
        { provide: AdminConfigService, useValue: adminConfig },
        {
          provide: UsbDeviceRelayService,
          useValue: { state: signal({ status: 'connected', serial: '127.0.0.1:5000', error: null }) }
        }
      ]
    }).compileComponents();
  });

  function create(): void {
    fixture = TestBed.createComponent(ComputersComponent);
    fixture.detectChanges();
  }

  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';
  const button = (label: string) =>
    Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find((b) =>
      b.textContent?.includes(label)
    ) as HTMLButtonElement | undefined;

  it('says computers are turned off when the server flag is off', () => {
    hosts.list.and.returnValue(of({ enabled: false, hosts: [], devices: [] }));
    create();
    expect(text()).toContain('Computers are turned off on this server.');
    expect(button('Connect a computer')).toBeUndefined();
  });

  it('shows an error with Retry that reloads', () => {
    hosts.list.and.returnValue(throwError(() => new HttpErrorResponse({ status: 500 })));
    create();
    expect(text()).toContain('Computers could not be loaded');
    hosts.list.and.returnValue(of(response([computer()])));
    button('Retry')!.click();
    fixture.detectChanges();
    expect(text()).toContain('Lab Mac');
  });

  it('shows the empty state with a Connect button for admins', () => {
    hosts.list.and.returnValue(of(response([])));
    create();
    expect(text()).toContain('No computers connected yet.');
    expect(button('Connect a computer')).toBeDefined();
  });

  it('hides every change action from non-admins', () => {
    adminConfig.getIdentity.and.returnValue(of(member));
    create();
    expect(text()).toContain('Lab Mac');
    expect(button('Connect a computer')).toBeUndefined();
    expect(button('Revoke')).toBeUndefined();
    expect(button('Rename')).toBeUndefined();
  });

  it('shows status, reason, since, phone counts and the exact share command', () => {
    create();
    expect(text()).toContain('Online');
    expect(text()).toContain('2 phones shared, 1 not shared');
    expect(text()).toContain('smartqa-host share emulator-5554');
    expect(text()).toContain('macOS 15');
    expect(text()).toContain('0.1.0');
  });

  it('shows the offline reason and how long ago it changed', () => {
    hosts.list.and.returnValue(
      of(response([computer({ status: 'offline', reason: 'timeout', since: Date.now() / 1000 - 125 })]))
    );
    create();
    expect(text()).toContain('Offline');
    expect(text()).toContain('Lost its connection.');
    expect(text()).toContain('2 min ago');
  });

  it('explains an offline computer whose session expired', () => {
    hosts.list.and.returnValue(
      of(response([computer({ status: 'offline', reason: 'auth_expired', since: Date.now() / 1000 })]))
    );
    create();
    expect(text()).toContain('Its session expired. Restart the computer software to reconnect.');
  });

  it('shows update required with the next step', () => {
    hosts.list.and.returnValue(
      of(response([computer({ status: 'update_required', reason: 'update_required' })]))
    );
    create();
    expect(text()).toContain('Update required');
    expect(text()).toContain('Needs a software update before it can connect.');
  });

  it('tells an online computer with no shared phones how to share one', () => {
    hosts.list.and.returnValue(
      of(response([computer({ phones_shared: 0, phones_not_shared: 1, unshared_serials: ['R5CT1'] })]))
    );
    create();
    expect(text()).toContain('0 phones shared, 1 not shared');
    expect(text()).toContain('smartqa-host share R5CT1');
  });

  it('lists phones with the computer name as their source', () => {
    hosts.list.and.returnValue(
      of(
        response(
          [computer()],
          [
            {
              serial: 'R5CT1',
              model: 'Pixel 8',
              source: 'computer',
              computer_id: 'h1',
              computer_name: 'Lab Mac',
              computer_status: 'online',
              reason: null,
              since: NOW
            },
            {
              serial: '127.0.0.1:5000',
              model: null,
              source: 'browser',
              computer_id: null,
              computer_name: null,
              computer_status: 'online',
              reason: null,
              since: null
            }
          ]
        )
      )
    );
    create();
    const chips = (fixture.nativeElement as HTMLElement).querySelectorAll('app-device-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toContain('Pixel 8');
    expect(chips[0].textContent).toContain('Lab Mac');
    expect(chips[1].textContent).toContain('This browser');
  });

  describe('revoke', () => {
    it('states how many active runs it will interrupt before confirming', () => {
      hosts.list.and.returnValue(of(response([computer({ active_run_count: 2 })])));
      create();
      button('Revoke')!.click();
      fixture.detectChanges();
      expect(text()).toContain('interrupts 2 active runs');
      expect(hosts.revoke).not.toHaveBeenCalled();
    });

    it('says no runs are affected when none are active, then revokes and refreshes', () => {
      hosts.revoke.and.returnValue(of({ status: 'revoked', interrupted_runs: 0 }));
      create();
      button('Revoke')!.click();
      fixture.detectChanges();
      expect(text()).toContain('No runs are active on it.');
      button('Revoke computer')!.click();
      fixture.detectChanges();
      expect(hosts.revoke).toHaveBeenCalledOnceWith('h1');
      expect(hosts.list).toHaveBeenCalledTimes(2);
    });

    it('does nothing when cancelled', () => {
      create();
      button('Revoke')!.click();
      fixture.detectChanges();
      button('Cancel')!.click();
      fixture.detectChanges();
      expect(hosts.revoke).not.toHaveBeenCalled();
      expect(text()).not.toContain('disconnects it now');
    });
  });

  describe('Connect a computer dialog', () => {
    const code = { code_id: 'c1', code: 'SECRETCODE', expires_at: Date.now() / 1000 + 900 };

    beforeEach(() => {
      hosts.list.and.returnValue(of(response([])));
      hosts.createCode.and.returnValue(of(code));
      hosts.codeStatus.and.returnValue(of({ status: 'waiting', computer_name: null }));
    });

    it('shows the code once with a countdown and a waiting state', fakeAsync(() => {
      create();
      button('Connect a computer')!.click();
      tick(0);
      fixture.detectChanges();
      const dialog = (fixture.nativeElement as HTMLElement).querySelector('[role="dialog"]')!;
      expect(dialog.getAttribute('aria-modal')).toBe('true');
      expect(dialog.textContent).toContain('SECRETCODE');
      expect(dialog.textContent).toContain('shown once');
      expect(dialog.textContent).toMatch(/1[45]:\d\d/);
      expect(dialog.textContent).toContain('Waiting for the computer…');
      discardPeriodicTasks();
    }));

    it('copies one install message that carries the code and its expiry', fakeAsync(() => {
      const write = jasmine.createSpy('writeText').and.returnValue(Promise.resolve());
      spyOnProperty(navigator, 'clipboard', 'get').and.returnValue({ writeText: write } as unknown as Clipboard);
      create();
      button('Connect a computer')!.click();
      tick(0);
      fixture.detectChanges();
      button('Copy install message')!.click();
      flushMicrotasks();
      const message = write.calls.mostRecent().args[0] as string;
      expect(message).toContain('SECRETCODE');
      expect(message).toContain('/api/agent/install.sh');
      expect(message).toContain('expires at');
      discardPeriodicTasks();
    }));

    it('turns into "<name> connected" and hides the code once the computer enrolls', fakeAsync(() => {
      create();
      button('Connect a computer')!.click();
      tick(0);
      hosts.codeStatus.and.returnValue(of({ status: 'connected', computer_name: 'Desk PC' }));
      tick(2000);
      fixture.detectChanges();
      const dialog = (fixture.nativeElement as HTMLElement).querySelector('[role="dialog"]')!;
      expect(dialog.textContent).toContain('Desk PC connected');
      expect(dialog.textContent).not.toContain('SECRETCODE');
      expect(hosts.list.calls.count()).toBeGreaterThan(1);
      discardPeriodicTasks();
    }));

    it('keeps waiting, with the code visible, when the computer only enrolled', fakeAsync(() => {
      create();
      button('Connect a computer')!.click();
      tick(0);
      hosts.codeStatus.and.returnValue(of({ status: 'enrolled', computer_name: 'Desk PC' }));
      tick(2000);
      fixture.detectChanges();
      const dialog = (fixture.nativeElement as HTMLElement).querySelector('[role="dialog"]')!;
      expect(dialog.textContent).not.toContain('Desk PC connected');
      expect(dialog.textContent).toContain('Desk PC enrolled. Waiting for it to connect…');
      expect(dialog.textContent).toContain('SECRETCODE');
      expect(hosts.list).toHaveBeenCalledTimes(1);
      // Only the authenticated handshake produces the success state.
      hosts.codeStatus.and.returnValue(of({ status: 'connected', computer_name: 'Desk PC' }));
      tick(2000);
      fixture.detectChanges();
      expect(dialog.textContent).toContain('Desk PC connected');
      discardPeriodicTasks();
    }));

    it('says the code expired in plain language', fakeAsync(() => {
      create();
      button('Connect a computer')!.click();
      tick(0);
      hosts.codeStatus.and.returnValue(of({ status: 'expired', computer_name: null }));
      tick(2000);
      fixture.detectChanges();
      expect(text()).toContain('This code expired. Create a new one.');
      discardPeriodicTasks();
    }));

    it('explains rate limits and non-admin refusals without error codes', fakeAsync(() => {
      create();
      hosts.createCode.and.returnValue(throwError(() => new HttpErrorResponse({ status: 429 })));
      button('Connect a computer')!.click();
      tick(0);
      fixture.detectChanges();
      expect(text()).toContain('Too many tries. Wait a minute and try again.');
      hosts.createCode.and.returnValue(throwError(() => new HttpErrorResponse({ status: 403 })));
      button('Connect a computer')!.click();
      tick(0);
      fixture.detectChanges();
      expect(text()).toContain('Only administrators can connect');
      discardPeriodicTasks();
    }));

    it('closes on Escape and returns focus to the button that opened it', fakeAsync(() => {
      create();
      const opener = button('Connect a computer')!;
      opener.focus();
      opener.click();
      tick(0);
      fixture.detectChanges();
      (fixture.nativeElement as HTMLElement)
        .querySelector('[role="dialog"]')!
        .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      fixture.detectChanges();
      expect((fixture.nativeElement as HTMLElement).querySelector('[role="dialog"]')).toBeNull();
      tick(0);
      expect(document.activeElement).toBe(opener);
      discardPeriodicTasks();
    }));

    it('counts down from the server expiry', () => {
      create();
      fixture.componentInstance.code.set({ ...code, expires_at: 2_000 });
      fixture.componentInstance.nowMs.set(2_000 * 1000 - 61_000);
      expect(fixture.componentInstance.countdown()).toBe('1:01');
      fixture.componentInstance.nowMs.set(2_000 * 1000 + 5);
      expect(fixture.componentInstance.countdown()).toBe('0:00');
    });
  });

  it('never uses the word daemon in any state a person can see', fakeAsync(() => {
    for (const status of ['online', 'offline', 'update_required', 'revoked'] as const) {
      hosts.list.and.returnValue(of(response([computer({ status, reason: status })])));
      create();
      expect(text().toLowerCase()).not.toContain('daemon');
      fixture.destroy();
    }
    hosts.list.and.returnValue(of({ enabled: false, hosts: [], devices: [] }));
    create();
    expect(text().toLowerCase()).not.toContain('daemon');
    flush();
  }));
});
