import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { phoneFakes } from '../../testing/phone-fakes';
import { of } from 'rxjs';
import { SystemService } from '../../services/system.service';
import { AdminConfigService } from '../../services/admin-config.service';
import {
  UsbDeviceRelayService,
  UsbDeviceRelayState
} from '../../services/usb-device-relay.service';
import { NavSwitcherComponent } from './nav-switcher.component';

describe('NavSwitcherComponent', () => {
  it('has no phone control: the Workspace chip is the only one', async () => {
    const relay = {
      state: signal<UsbDeviceRelayState>({ status: 'connected', serial: 'R58M123', sessionId: 's1', error: null }),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.usb-relay-badge')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Phone connected');
    expect(fixture.nativeElement.textContent).not.toContain('Disconnect');
  });

  it('links to the run library between Workspace and System Setup', async () => {
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    const hrefs = Array.from<HTMLAnchorElement>(fixture.nativeElement.querySelectorAll('nav a[href]')).map(a =>
      a.getAttribute('href')
    );
    expect(hrefs).toEqual(['/workspace', '/runs', '/setup']);
    expect(fixture.nativeElement.querySelector('a[href="/runs"]').textContent).toContain('Runs');
    // Labels collapse to icons on phones, so each tab needs its own accessible name.
    const names = Array.from<HTMLAnchorElement>(fixture.nativeElement.querySelectorAll('nav a[href]')).map(a =>
      a.getAttribute('aria-label')
    );
    expect(names).toEqual(['Workspace', 'Runs', 'System Setup']);
  });

  it('stays inside the viewport (CHE-1189)', async () => {
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    // The identity wraps onto a second row instead of running off-screen.
    const nav = fixture.nativeElement.querySelector('.floating-nav-switcher') as HTMLElement;
    expect(nav.getBoundingClientRect().right).toBeLessThanOrEqual(window.innerWidth);
    expect(getComputedStyle(nav).flexWrap).toBe('wrap');
  });

  it("hides What's New navigation when there are no entries", async () => {

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();

    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[aria-label="Open What\'s New"]')).toBeNull();
  });

  it('announces unread updates in the navigation', async () => {

    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
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
        ...phoneFakes().providers,
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

  it('puts the phone chip in the top bar, the first click of the 2-click connect (CHE-1143, OCR F6)', async () => {
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    const nav = fixture.nativeElement.querySelector('nav.floating-nav-switcher') as HTMLElement;
    const chip = nav.querySelector('app-workspace-device-chip button.chip') as HTMLButtonElement;
    expect(chip).not.toBeNull();
    expect(chip.textContent).toContain('No phone');
    // It sits in the top bar's status area, next to who is signed in.
    expect(chip.closest('.nav-status')).not.toBeNull();
    expect(nav.querySelectorAll('app-workspace-device-chip').length).toBe(1);
    expect(nav.getBoundingClientRect().top).toBeLessThan(window.innerHeight / 4);
  });
});
