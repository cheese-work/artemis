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
});
