import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { provideRouter, Router } from '@angular/router';
import { Component } from '@angular/core';
import { phone, phoneFakes } from '../../testing/phone-fakes';
import { of } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { NavSwitcherComponent } from './nav-switcher.component';
import { expectHitBox } from '../../testing/hit-box';

@Component({ template: '' })
class PageStub {}

describe('NavSwitcherComponent', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [{ provide: HttpClient, useValue: { get: () => of(null) } }, {
        provide: AdminConfigService,
        useValue: { getIdentity: () => of({ email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null }) }
      }]
    });
  });

  it('has no phone control: the Workspace chip is the only one', async () => {
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

  it('shows Workspace and Runs, with no Setup tab for a non-admin', async () => {
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();

    const hrefs = Array.from<HTMLAnchorElement>(fixture.nativeElement.querySelectorAll('nav a[href]')).map(a =>
      a.getAttribute('href')
    );
    expect(hrefs).toEqual(['/workspace', '/runs']);
    expect(fixture.nativeElement.querySelector('a[href="/runs"]').textContent).toContain('Runs');
    // Labels collapse to icons on phones, so each tab needs its own accessible name.
    const names = Array.from<HTMLAnchorElement>(fixture.nativeElement.querySelectorAll('nav a[href]')).map(a =>
      a.getAttribute('aria-label')
    );
    expect(names).toEqual(['Workspace', 'Runs']);
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
    expect(getComputedStyle(nav).flexWrap).toBe(window.innerWidth >= 1024 ? 'nowrap' : 'wrap');
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

  it("keeps Setup in the admin menu and adds desktop navigation with unread What's New", async () => {
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
    expect(fixture.nativeElement.querySelector('nav > a[href="/setup"]')?.textContent).toContain('Setup');
    const userMenu = fixture.nativeElement.querySelector('app-admin-identity-indicator details') as HTMLDetailsElement;
    expect(userMenu).not.toBeNull();
    if (!userMenu) return;
    expect(userMenu.open).toBeFalse();
    userMenu.querySelector('summary')!.click();
    expect(userMenu.open).toBeTrue();
    expect(userMenu.querySelector('a[href="/setup"]')?.textContent?.trim()).toBe('Setup');
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

  it("orders Workspace, Runs, What's New, the device chip and the user", async () => {
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...phoneFakes().providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.componentRef.setInput('hasWhatsNew', true);
    fixture.detectChanges();
    const controls = Array.from<HTMLElement>(fixture.nativeElement.querySelectorAll('nav > a, nav > button, .nav-status > *'));
    expect(controls.map(control => control.getAttribute('aria-label') || control.tagName.toLowerCase()))
      .toEqual(['Workspace', 'Runs', "Open What's New", 'app-workspace-device-chip', 'app-admin-identity-indicator']);
  });

  for (const path of ['/workspace', '/runs']) {
    it(`reaches browser connect in exactly 2 clicks from ${path}, without Setup or scrolling`, async () => {
      const fakes = phoneFakes();
      await TestBed.configureTestingModule({
        imports: [NavSwitcherComponent],
        providers: [provideRouter([
          { path: 'workspace', component: PageStub },
          { path: 'runs', component: PageStub }
        ]), ...fakes.providers]
      }).compileComponents();
      const router = TestBed.inject(Router);
      await router.navigateByUrl(path);
      const fixture = TestBed.createComponent(NavSwitcherComponent);
      fixture.detectChanges();
      const root = fixture.nativeElement as HTMLElement;
      const chip = root.querySelector<HTMLButtonElement>('button.chip')!;
      chip.click();
      fixture.detectChanges();
      const connect = Array.from(root.querySelectorAll<HTMLButtonElement>('.panel button'))
        .find(button => button.textContent!.includes('Connect a phone from this browser'))!;
      expect(connect.disabled).toBeFalse();
      expect(connect.getBoundingClientRect().bottom).toBeLessThanOrEqual(window.innerHeight);
      connect.click();
      fixture.detectChanges();
      expect(fakes.relay.connect).toHaveBeenCalledTimes(1);
      expect(router.url).toBe(path);
      expect(root.querySelector('.panel')).toBeNull();
      expect(fakes.system.restartAdb).not.toHaveBeenCalled();
    });
  }

  it('keeps raw phone addresses out of the chip and shows them in sidebar and picker details', async () => {
    const fakes = phoneFakes();
    const serial = '127.0.0.1:41003';
    fakes.relay.state.set({ status: 'connected', serial, sessionId: 'bridge', error: null });
    fakes.system.connectedDevices.set([phone({ serial })]);
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent],
      providers: [provideRouter([]), ...fakes.providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const chip = root.querySelector<HTMLButtonElement>('button.chip')!;
    expect(chip.textContent).toContain('Pixel 6 · Phone');
    expect(root.querySelector('.phone-card-serial')?.textContent).toContain(serial);
    chip.click();
    fixture.detectChanges();
    expect(root.querySelector('.device-detail')?.textContent).toContain(serial);
    expect(root.querySelector('.option-text')?.textContent).not.toContain(serial);
  });

  it('shows the scoped run count and the word New with non-overlapping 44 px controls', async () => {
    const fakes = phoneFakes();
    fakes.agent.sessions.set([{ session_id: 'one', status: 'completed' }, { session_id: 'two', status: 'pending' }]);
    await TestBed.configureTestingModule({
      imports: [NavSwitcherComponent], providers: [provideRouter([]), ...fakes.providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(NavSwitcherComponent);
    fixture.componentRef.setInput('hasWhatsNew', true);
    fixture.componentRef.setInput('hasUnreadWhatsNew', true);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    expect(root.querySelector('.run-count')?.textContent?.trim()).toBe('2');
    expect(root.querySelector('.nav-new-label')?.textContent?.trim()).toBe('New');
    const controls = Array.from(root.querySelectorAll<HTMLElement>('nav > a, nav > button, button.chip, summary'));
    for (const control of controls) expectHitBox(control);
    const navItems = controls.filter(control => control.matches('.nav-tab-btn'));
    for (let index = 1; index < navItems.length; index++) {
      const previous = navItems[index - 1].getBoundingClientRect();
      const current = navItems[index].getBoundingClientRect();
      expect(previous.bottom <= current.top || previous.right <= current.left).toBeTrue();
    }
  });
});
