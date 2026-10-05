import { Computer, RegistryDevice } from '../core/models/host.model';
import { deviceChipView } from './device-chip.util';

const computer = (over: Partial<Computer> = {}): Computer => ({
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
  active_run_count: 0,
  ...over
});

const phone = (over: Partial<RegistryDevice> = {}): RegistryDevice => ({
  serial: 'R5CT1',
  model: 'Pixel 8',
  source: 'computer',
  computer_id: 'h1',
  computer_name: 'Lab Mac',
  computer_status: 'online',
  reason: null,
  since: 1_800_000_000,
  ...over
});

describe('deviceChipView', () => {
  it('shows this tab\'s own browser phone as ready from This browser', () => {
    const own = phone({ source: 'browser', computer_id: null, computer_name: null, serial: '127.0.0.1:5000' });
    const view = deviceChipView(own, [], '127.0.0.1:5000');
    expect(view).toEqual(jasmine.objectContaining({ state: 'Ready', source: 'This browser' }));
    expect(view.detail).toContain('Keep this browser tab open');
  });

  it('calls another browser\'s phone "A browser" with no current-tab instruction', () => {
    const other = phone({ source: 'browser', computer_id: null, computer_name: null, serial: '127.0.0.1:6000' });
    for (const own of ['127.0.0.1:5000', null]) {
      const view = deviceChipView(other, [], own);
      expect(view.source).toBe('A browser');
      expect(view.detail).not.toContain('this browser tab');
      expect(view.detail).not.toContain('Keep');
    }
  });

  it('shows the computer name as the source of a phone on an online computer', () => {
    const view = deviceChipView(phone(), [computer()]);
    expect(view.state).toBe('Ready');
    expect(view.source).toBe('Lab Mac');
    expect(view.label).toBe('Pixel 8');
  });

  it('falls back to the serial when the model of a computer phone is unknown', () => {
    expect(deviceChipView(phone({ model: null }), [computer()]).label).toBe('R5CT1');
  });

  it('labels a browser phone by model and kind, never by its loopback address', () => {
    const browser = phone({
      source: 'browser',
      computer_id: null,
      computer_name: null,
      serial: '127.0.0.1:36411',
      model: '21081111RG',
      device_kind: 'phone'
    });
    const view = deviceChipView(browser, [], null);
    expect(view.label).toBe('21081111RG');
    expect(view.kind).toBe('Phone');
  });

  it('calls an unclassified loopback phone Unknown device, not its address or Virtual Device', () => {
    const browser = phone({
      source: 'browser',
      computer_id: null,
      computer_name: null,
      serial: '127.0.0.1:40001',
      model: null,
      device_kind: 'unknown'
    });
    const view = deviceChipView(browser, [], null);
    expect(view.label).toBe('Unknown device');
    expect(view.kind).toBeNull();
  });

  it('shows a real emulator as an emulator', () => {
    const emulator = phone({ serial: 'emulator-5554', model: 'sdk_gphone64_arm64', device_kind: 'emulator' });
    expect(deviceChipView(emulator, [computer()]).kind).toBe('Emulator');
  });

  it('shows no kind for a computer phone the agent did not classify', () => {
    expect(deviceChipView(phone(), [computer()]).kind).toBeNull();
  });

  it('marks phones on an offline computer offline with the reason and since', () => {
    const view = deviceChipView(
      phone({ computer_status: 'offline', reason: 'timeout', since: 1_800_000_000 }),
      [computer({ status: 'offline', reason: 'timeout' })]
    );
    expect(view.state).toBe('Offline');
    expect(view.detail).toContain('Lab Mac lost its connection');
    expect(view.since).toBe(1_800_000_000);
  });

  it('propagates update required to the phone with a plain next step', () => {
    const view = deviceChipView(phone({ computer_status: 'update_required' }), [
      computer({ status: 'update_required', reason: 'update_required' })
    ]);
    expect(view.state).toBe('Needs attention');
    expect(view.detail).toContain('Update the software on Lab Mac');
  });

  it('treats a revoked computer as offline and says it was removed', () => {
    const view = deviceChipView(phone({ computer_status: 'revoked', reason: 'revoked' }), [
      computer({ status: 'revoked', reason: 'revoked' })
    ]);
    expect(view.state).toBe('Offline');
    expect(view.detail).toContain('removed');
  });

  it('never conveys state by colour alone: every view carries text and an icon name', () => {
    for (const status of ['online', 'offline', 'update_required', 'revoked'] as const) {
      const view = deviceChipView(phone({ computer_status: status }), [computer({ status })]);
      expect(view.state.length).toBeGreaterThan(0);
      expect(view.icon.length).toBeGreaterThan(0);
    }
  });
});
