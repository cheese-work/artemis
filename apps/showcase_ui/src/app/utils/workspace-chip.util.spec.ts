import { DeviceInfo } from '../core/models/system.model';
import { UsbDeviceRelayState } from '../services/usb-device-relay.service';
import { pickerOptions, WorkspaceChipInput, workspaceChipView } from './workspace-chip.util';

const idle: UsbDeviceRelayState = { status: 'idle', serial: null, sessionId: null, error: null };
const phone = (over: Partial<DeviceInfo> = {}): DeviceInfo => ({
  serial: 'R58M1234a1b2',
  state: 'device',
  model: 'Pixel 6',
  product: null,
  android_version: null,
  screen_resolution: null,
  is_locked: false,
  is_emulator: false,
  device_kind: 'phone',
  ...over
});
const input = (over: Partial<WorkspaceChipInput> = {}): WorkspaceChipInput => ({
  relay: idle,
  webUsbSupported: true,
  devices: [],
  registry: [],
  computers: [],
  selected: null,
  runInterrupted: false,
  attaching: false,
  otherTab: null,
  ...over
});

describe('workspaceChipView', () => {
  it('has no phone to start with and offers no run target', () => {
    const view = workspaceChipView(input());
    expect(view.kind).toBe('none');
    expect(view.text).toBe('No phone · Connect');
    expect(view.target).toBeNull();
  });

  it('never picks a listed phone by itself', () => {
    expect(workspaceChipView(input({ devices: [phone()] })).kind).toBe('none');
  });

  it('says so when the browser cannot reach a phone', () => {
    expect(workspaceChipView(input({ webUsbSupported: false })).hint).toBe('Use Chrome on a computer to connect a phone.');
  });

  it('shows connecting while the chooser or bridge is open', () => {
    const view = workspaceChipView(input({ relay: { ...idle, status: 'connecting' }, selected: 'R58M1234a1b2', devices: [phone()] }));
    expect(view.kind).toBe('connecting');
    expect(view.target).toBeNull();
  });

  it('binds a phone held by this tab to its bridge session', () => {
    const relay: UsbDeviceRelayState = { status: 'connected', serial: '127.0.0.1:41003', sessionId: 'bridge-9', error: null };
    const view = workspaceChipView(input({ relay, devices: [phone({ serial: '127.0.0.1:41003' })] }));
    expect(view.kind).toBe('connected');
    expect(view.text).toBe('Pixel 6 · …1003 · via this browser');
    expect(view.target).toEqual({ serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' });
  });

  describe('a selected phone that is unavailable (OCR F1)', () => {
    const own: UsbDeviceRelayState = { status: 'connected', serial: '127.0.0.1:41003', sessionId: 'bridge-9', error: null };
    const browserPhone = phone({ serial: '127.0.0.1:41003', model: 'Pixel 6' });
    const shared = (over: Partial<DeviceInfo> = {}) => phone({ serial: 'emulator-5554', model: 'Pixel 8', ...over });

    it('runs on the explicitly picked shared phone while this browser also holds another', () => {
      const view = workspaceChipView(input({ relay: own, selected: 'emulator-5554', devices: [browserPhone, shared()] }));
      expect(view.kind).toBe('connected');
      expect(view.target).toEqual({ serial: 'emulator-5554', bridgeSessionId: null });
    });

    it('returns no runnable target when the picked phone is gone, and never falls back to the browser phone', () => {
      const view = workspaceChipView(input({ relay: own, selected: 'emulator-5554', devices: [browserPhone] }));
      expect(view.kind).toBe('none');
      expect(view.target).toBeNull();
      expect(view.text).toBe('Phone not available');
    });

    it('returns no runnable target when the picked phone is offline', () => {
      const view = workspaceChipView(
        input({ relay: own, selected: 'emulator-5554', devices: [browserPhone, shared({ state: 'offline' })] })
      );
      expect(view.target).toBeNull();
      expect(view.text).toBe('Phone not available');
    });

    it('returns no runnable target when the picked phone is unauthorized, and asks to allow it', () => {
      const view = workspaceChipView(
        input({ relay: own, selected: 'emulator-5554', devices: [browserPhone, shared({ state: 'unauthorized' })] })
      );
      expect(view.target).toBeNull();
      expect(view.text).toBe('Allow USB debugging');
    });

    it('runs on the browser phone once the person picks it', () => {
      const view = workspaceChipView(input({ relay: own, selected: '127.0.0.1:41003', devices: [browserPhone] }));
      expect(view.target).toEqual({ serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' });
    });
  });

  it('shows Attaching… once the chooser is done and the bridge has not reported the phone yet', () => {
    const connecting: UsbDeviceRelayState = { ...idle, status: 'connecting' };
    const chooser = workspaceChipView(input({ relay: connecting }));
    const attaching = workspaceChipView(input({ relay: connecting, attaching: true }));
    expect(chooser.text).toBe('Connecting…');
    expect(attaching.kind).toBe('connecting');
    expect(attaching.text).toBe('Attaching…');
    expect(attaching.hint).toBe('Unlock the phone and tap Allow.');
    expect(attaching.target).toBeNull();
  });

  it('shows "Connected in another tab" with no run target when another tab holds the phone', () => {
    const view = workspaceChipView(
      input({ otherTab: { serial: '127.0.0.1:41003' }, selected: '127.0.0.1:41003', devices: [phone({ serial: '127.0.0.1:41003' })] })
    );
    expect(view.kind).toBe('other-tab');
    expect(view.text).toBe('Connected in another tab');
    expect(view.hint).toContain('Use here');
    expect(view.target).toBeNull();
  });

  it('does not treat the phone this tab holds as being in another tab', () => {
    const relay: UsbDeviceRelayState = { status: 'connected', serial: '127.0.0.1:41003', sessionId: 'b', error: null };
    expect(workspaceChipView(input({ relay, otherTab: { serial: '127.0.0.1:41003' } })).kind).toBe('connected');
  });

  it('counts the tab’s phone as connected before the adb list shows it', () => {
    const relay: UsbDeviceRelayState = { status: 'connected', serial: '127.0.0.1:41003', sessionId: 'bridge-9', error: null };
    expect(workspaceChipView(input({ relay })).kind).toBe('connected');
  });

  it('names the computer for a shared phone, with no bridge session', () => {
    const view = workspaceChipView(
      input({
        selected: 'R58M1234a1b2',
        devices: [phone()],
        computers: [{ id: 'c1', name: 'X99' } as never],
        registry: [
          { serial: 'R58M1234a1b2', source: 'computer', computer_id: 'c1', computer_name: 'X99', computer_status: 'online' } as never
        ]
      })
    );
    expect(view.text).toBe('Pixel 6 · …a1b2 · via X99');
    expect(view.target).toEqual({ serial: 'R58M1234a1b2', bridgeSessionId: null });
  });

  it('labels an emulator', () => {
    const emulator = phone({ serial: 'emulator-5554', model: 'sdk_gphone', is_emulator: true, device_kind: 'emulator' });
    const view = workspaceChipView(input({ selected: 'emulator-5554', devices: [emulator] }));
    expect(view.text).toBe('sdk_gphone · …5554 · emulator');
  });

  it('drops a remembered phone that is no longer listed', () => {
    expect(workspaceChipView(input({ selected: 'gone', devices: [phone()] })).kind).toBe('none');
  });

  it('asks to approve USB debugging for a remembered phone that is not allowed yet', () => {
    const view = workspaceChipView(input({ selected: 'R58M1234a1b2', devices: [phone({ state: 'unauthorized' })] }));
    expect(view.kind).toBe('none');
    expect(view.text).toBe('Allow USB debugging');
  });

  it('shows dropped after a connected bridge is lost, with the reason', () => {
    const relay: UsbDeviceRelayState = { ...idle, status: 'dropped', error: 'The phone disconnected. Reconnect it to continue.' };
    const view = workspaceChipView(input({ relay }));
    expect(view.kind).toBe('dropped');
    expect(view.text).toBe('Phone disconnected');
    expect(view.hint).toBe('Reconnect the phone to run.');
    expect(view.target).toBeNull();
  });

  it('shows interrupted when the run ended without its phone', () => {
    const view = workspaceChipView(input({ runInterrupted: true }));
    expect(view.kind).toBe('interrupted');
    expect(view.target).toBeNull();
  });

  it('prefers a live phone over a stale interruption', () => {
    const relay: UsbDeviceRelayState = { status: 'connected', serial: '127.0.0.1:1', sessionId: 's', error: null };
    expect(workspaceChipView(input({ relay, runInterrupted: true })).kind).toBe('connected');
  });
});

describe('pickerOptions', () => {
  it('lists every phone with a note for the ones that cannot run yet', () => {
    const options = pickerOptions(
      input({
        devices: [
          phone(),
          phone({ serial: 'B', state: 'unauthorized', model: null }),
          phone({ serial: 'C', state: 'offline' }),
          phone({ serial: 'D', is_locked: true })
        ]
      })
    );
    expect(options.map((option) => option.note)).toEqual([
      null,
      'Unlock the phone and tap Allow.',
      'The phone is offline.',
      'Unlock the phone to run.'
    ]);
    expect(options[0].text).toBe('Pixel 6 · …a1b2');
  });
});
