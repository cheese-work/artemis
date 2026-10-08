import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import {
  UsbDeviceRelayService,
  UsbDeviceRelayState
} from '../../services/usb-device-relay.service';
import { UsbPhoneConnectionComponent } from './usb-phone-connection.component';

describe('UsbPhoneConnectionComponent', () => {
  let fixture: ComponentFixture<UsbPhoneConnectionComponent>;
  let relay: {
    isSupported: ReturnType<typeof signal<boolean>>;
    state: ReturnType<typeof signal<UsbDeviceRelayState>>;
    connect: jasmine.Spy;
    disconnect: jasmine.Spy;
  };

  beforeEach(async () => {
    relay = {
      isSupported: signal(true),
      state: signal<UsbDeviceRelayState>({ status: 'idle', serial: null, sessionId: null, error: null }),
      connect: jasmine.createSpy('connect').and.resolveTo(undefined),
      disconnect: jasmine.createSpy('disconnect').and.resolveTo(undefined)
    };

    await TestBed.configureTestingModule({
      imports: [UsbPhoneConnectionComponent],
      providers: [{ provide: UsbDeviceRelayService, useValue: relay }]
    }).compileComponents();

    fixture = TestBed.createComponent(UsbPhoneConnectionComponent);
    fixture.detectChanges();
  });

  it('shows the connect action and unsupported-browser guidance', () => {
    relay.isSupported.set(false);
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Use desktop Chromium');
    expect(fixture.nativeElement.querySelector('button').textContent).toContain(
      'Connect phone from this browser'
    );
  });

  it('disables the connect action while permission and bridge setup are in progress', () => {
    relay.state.set({ status: 'connecting', serial: null, sessionId: null, error: null });
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
    expect(button.disabled).toBeTrue();
    expect(button.textContent).toContain('Connecting');
    expect(fixture.nativeElement.textContent).toContain('permission prompt');
  });

  it('announces permission and connection errors accessibly', () => {
    relay.state.set({
      status: 'error',
      serial: null,
      sessionId: null,
      error: 'USB permission was not granted. Allow access to the phone and try again.'
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="alert"]')?.textContent).toContain(
      'USB permission was not granted'
    );
  });

  it('shows unexpected error details on a separate small alert line', () => {
    relay.state.set({
      status: 'error',
      serial: null,
      sessionId: null,
      error: 'Could not connect the phone. Check its cable and USB Debugging, then retry.\n' +
        'Details: InvalidStateError: The interface is unavailable.\nUSB transfer failed.'
    });
    fixture.detectChanges();

    const alert = fixture.nativeElement.querySelector('[role="alert"]') as HTMLElement;
    const details = alert.querySelector('small') as HTMLElement;
    expect(alert.textContent).toContain('Could not connect the phone');
    expect(details.textContent).toBe(
      'Details: InvalidStateError: The interface is unavailable.\nUSB transfer failed.'
    );
    expect(getComputedStyle(details).whiteSpace).toBe('pre-line');
  });

  it('shows the attached phone serial and tab-lifetime warning', () => {
    relay.state.set({ status: 'connected', serial: 'R58M123', sessionId: null, error: null });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('code')?.textContent).toBe('R58M123');
    expect(fixture.nativeElement.textContent).toContain('Keep this tab open');
    expect(fixture.nativeElement.querySelector('button')).toBeNull();
  });
});
