import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import {
  UsbDeviceRelayService,
  UsbDeviceRelayState
} from '../../services/usb-device-relay.service';
import { NavSwitcherComponent } from './nav-switcher.component';

describe('NavSwitcherComponent USB relay badge', () => {
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
    fixture.componentRef.setInput('hasWhatsNew', true);
    fixture.componentRef.setInput('hasUnreadWhatsNew', true);
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector('button[aria-label="Open What\'s New, unread updates"]');
    expect(button).not.toBeNull();
    expect(button.querySelector('.nav-unread-indicator')).not.toBeNull();
  });
});
