import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import {
  UsbDeviceRelayService,
  UsbDeviceRelayState
} from '../../services/usb-device-relay.service';
import { NavSwitcherComponent } from './nav-switcher.component';

describe('NavSwitcherComponent', () => {
  it('shows the browser phone and disconnect action while connected', async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'connected', serial: 'R58M123', error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [
        provideRouter([]),
        { provide: UsbDeviceRelayService, useValue: relay }
      ]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Phone connected via this browser');
    expect(fixture.nativeElement.querySelector('code')?.textContent).toBe('R58M123');
    (fixture.nativeElement.querySelector('.usb-relay-badge button') as HTMLButtonElement).click();
    expect(relay.disconnect).toHaveBeenCalled();
  });

  it('links to the run library between Workspace and System Setup', async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'idle', serial: null, error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), { provide: UsbDeviceRelayService, useValue: relay }]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    const hrefs = Array.from<HTMLAnchorElement>(fixture.nativeElement.querySelectorAll('nav a[href]')).map(a =>
      a.getAttribute('href')
    );
    expect(hrefs).toEqual(['/workspace', '/runs', '/setup']);
    expect(fixture.nativeElement.querySelector('a[href="/runs"]').textContent).toContain('Runs');
  });

  it("hides What's New navigation when there are no entries", async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'idle', serial: null, error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), { provide: UsbDeviceRelayService, useValue: relay }]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[aria-label="Open What\'s New"]')).toBeNull();
  });

  it('announces unread updates in the navigation', async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'idle', serial: null, error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), { provide: UsbDeviceRelayService, useValue: relay }]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    const showWhatsNew = jasmine.createSpy('showWhatsNew');
    fixture.componentRef.setInput('hasWhatsNew', true);
    fixture.componentRef.setInput('hasUnreadWhatsNew', true);
    fixture.componentInstance.showWhatsNew.subscribe(showWhatsNew);
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector(
      '[aria-label="Open What\'s New, unread updates"]'
    ) as HTMLButtonElement;
    expect(button).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.nav-unread-indicator')).not.toBeNull();
    button.click();
    expect(showWhatsNew).toHaveBeenCalled();
  });

  it("retains System Setup and unread What's New navigation with admin identity", async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'idle', serial: null, error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };
    const adminConfig = {
      getIdentity: () => of({
        email: 'admin@example.test',
        admin: true,
        auth_mode: 'cloudflare' as const,
        reason: null
      })
    };

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [
        provideRouter([]),
        { provide: UsbDeviceRelayService, useValue: relay },
        { provide: AdminConfigService, useValue: adminConfig }
      ]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    const showWhatsNew = jasmine.createSpy('showWhatsNew');
    fixture.componentInstance.hasWhatsNew = true;
    fixture.componentInstance.hasUnreadWhatsNew = true;
    fixture.componentInstance.showWhatsNew.subscribe(showWhatsNew);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.brand-wordmark')?.textContent).toContain('SmartQA');
    expect(fixture.nativeElement.querySelector('a[href="/setup"]')?.textContent).toContain('System Setup');
    const whatsNewButton = fixture.nativeElement.querySelector(
      `[aria-label="Open What's New, unread updates"]`
    ) as HTMLButtonElement;
    expect(whatsNewButton).not.toBeNull();
    expect(fixture.nativeElement.querySelector('.nav-unread-indicator')).not.toBeNull();
    whatsNewButton.click();
    expect(showWhatsNew).toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('app-admin-identity-indicator')?.textContent).toContain('Admin');
  });
});
