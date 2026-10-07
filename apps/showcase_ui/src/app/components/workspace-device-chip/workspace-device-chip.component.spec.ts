import { TestBed } from '@angular/core/testing';
import { phone, phoneFakes } from '../../testing/phone-fakes';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { WorkspaceDeviceChipComponent } from './workspace-device-chip.component';

describe('WorkspaceDeviceChipComponent', () => {
  let fakes: ReturnType<typeof phoneFakes>;

  function create() {
    TestBed.configureTestingModule({ providers: fakes.providers });
    const fixture = TestBed.createComponent(WorkspaceDeviceChipComponent);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const chip = () => el.querySelector<HTMLButtonElement>('button.chip')!;
    const panel = () => el.querySelector<HTMLElement>('.panel');
    const buttons = () => Array.from(el.querySelectorAll<HTMLButtonElement>('.panel button'));
    const settle = () => { fixture.detectChanges(); TestBed.tick(); fixture.detectChanges(); };
    return { fixture, el, chip, panel, buttons, settle };
  }

  beforeEach(() => (fakes = phoneFakes()));

  /** A run in progress on `serial`, while the person may be looking at some other run. */
  const runningOn = (serial: string | null) => {
    fakes.agent.sessions.update((all) => [...all, { session_id: 'live', status: 'running', device_serial: serial }]);
    fakes.agent.agentStatus.set('running');
  };
  const viewing = (status: string) => fakes.agent.currentSession.set({ status, initial_goal: 'old' });

  it('starts as "No phone · Connect" with the picker closed', () => {
    const { chip, panel } = create();
    expect(chip().textContent).toContain('No phone · Connect');
    expect(chip().getAttribute('aria-expanded')).toBe('false');
    expect(panel()).toBeNull();
  });

  it('opens the picker with the device list, Connect from this browser, and no Disconnect', () => {
    fakes.system.connectedDevices.set([phone(), phone({ serial: 'B', model: 'Pixel 8' })]);
    const { chip, buttons, settle } = create();
    chip().click();
    settle();
    expect(chip().getAttribute('aria-expanded')).toBe('true');
    const labels = buttons().map((b) => b.textContent!.replace(/\s+/g, ' ').trim());
    expect(labels.length).toBe(3);
    expect(labels[0]).toBe('Pixel 6 · …a1b2');
    expect(labels[1]).toBe('Pixel 8 · …B');
    expect(labels[2]).toContain('Connect from this browser');
  });

  it('picks a listed phone for the next run and closes with focus back on the chip', () => {
    fakes.system.connectedDevices.set([phone()]);
    const { fixture, chip, buttons, panel, settle } = create();
    document.body.appendChild(fixture.nativeElement);
    chip().click();
    settle();
    buttons()[0].click();
    settle();
    expect(fakes.system.chooseRunTarget).toHaveBeenCalledWith('R58M1234a1b2');
    expect(panel()).toBeNull();
    expect(chip().textContent).toContain('Pixel 6 · …a1b2');
    expect(document.activeElement).toBe(chip());
    fixture.nativeElement.remove();
  });

  it('moves focus into the picker when it opens', () => {
    fakes.system.connectedDevices.set([phone()]);
    const { fixture, chip, panel, settle } = create();
    document.body.appendChild(fixture.nativeElement);
    chip().click();
    settle();
    expect(panel()!.contains(document.activeElement)).toBeTrue();
    fixture.nativeElement.remove();
  });

  it('closes on Escape and returns focus to the chip', () => {
    const { fixture, chip, panel, settle } = create();
    document.body.appendChild(fixture.nativeElement);
    chip().click();
    settle();
    panel()!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    settle();
    expect(panel()).toBeNull();
    expect(document.activeElement).toBe(chip());
    fixture.nativeElement.remove();
  });

  it('connects from this browser and picks that phone', () => {
    const { chip, buttons, settle } = create();
    chip().click();
    settle();
    buttons().find((b) => b.textContent!.includes('Connect from this browser'))!.click();
    expect(fakes.relay.connect).toHaveBeenCalled();
    fakes.relay.state.set({ status: 'connected', serial: '127.0.0.1:41003', sessionId: 'bridge-9', error: null });
    settle();
    expect(fakes.system.chooseRunTarget).toHaveBeenCalledWith('127.0.0.1:41003');
    expect(chip().textContent).toContain('via this browser');
    expect(TestBed.inject(WorkspacePhoneService).target()).toEqual({ serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' });
  });

  it('shows connecting, and cannot start a second connect meanwhile', () => {
    fakes.relay.state.set({ ...fakes.relay.state(), status: 'connecting' });
    const { chip, buttons, settle } = create();
    expect(chip().textContent).toContain('Connecting…');
    chip().click();
    settle();
    expect(buttons().find((b) => b.textContent!.includes('Connect from this browser'))!.disabled).toBeTrue();
  });

  it('offers no browser connect where WebUSB is unavailable, and says why', () => {
    fakes.relay.isSupported.set(false);
    const { chip, buttons, el, settle } = create();
    chip().click();
    settle();
    expect(buttons().find((b) => b.textContent!.includes('Connect from this browser'))!.disabled).toBeTrue();
    expect(el.querySelector('.hint')!.textContent).toContain('Use Chrome on a computer');
  });

  it('shows dropped with the reason, and Connect brings the phone back', () => {
    fakes.relay.state.set({ status: 'dropped', serial: null, sessionId: null, error: 'The phone disconnected. Reconnect it to continue.' });
    const { chip, el, buttons, settle } = create();
    expect(chip().textContent).toContain('Phone disconnected');
    chip().click();
    settle();
    expect(el.querySelector('.error')!.textContent).toContain('The phone disconnected');
    buttons().find((b) => b.textContent!.includes('Connect from this browser'))!.click();
    expect(fakes.relay.connect).toHaveBeenCalled();
  });

  it('disconnects the browser phone, asking first while it is running a run', () => {
    fakes.relay.state.set({ status: 'connected', serial: '127.0.0.1:41003', sessionId: 'b', error: null });
    const { chip, buttons, settle } = create();
    chip().click();
    settle();
    buttons().find((b) => b.textContent!.trim() === 'Disconnect')!.click();
    expect(fakes.relay.disconnect).toHaveBeenCalledTimes(1);

    runningOn('127.0.0.1:41003');
    settle();
    chip().click();
    settle();
    buttons().find((b) => b.textContent!.trim() === 'Disconnect')!.click();
    settle();
    expect(fakes.relay.disconnect).toHaveBeenCalledTimes(1);
    expect(buttons().map((b) => b.textContent!.trim())).toContain('Disconnect and stop');
    buttons().find((b) => b.textContent!.trim() === 'Disconnect and stop')!.click();
    expect(fakes.relay.disconnect).toHaveBeenCalledTimes(2);
  });

  it('locks the phone list while a run is on it', () => {
    fakes.system.connectedDevices.set([phone()]);
    fakes.system.selectedRunTarget.set('R58M1234a1b2');
    runningOn('R58M1234a1b2');
    const { chip, el, buttons, settle } = create();
    chip().click();
    settle();
    expect(buttons()[0].disabled).toBeTrue();
    expect(el.querySelector('.panel')!.textContent).toContain('Phone in use by this run.');
  });

  it('disables phones that wait for approval or are offline, with the reason beside them', () => {
    fakes.system.connectedDevices.set([phone({ state: 'unauthorized' }), phone({ serial: 'C', state: 'offline' })]);
    const { chip, el, buttons, settle } = create();
    chip().click();
    settle();
    expect(buttons()[0].disabled).toBeTrue();
    expect(buttons()[1].disabled).toBeTrue();
    expect(el.querySelector('.panel')!.textContent).toContain('Unlock the phone and tap Allow.');
  });

  it('opens when Run asks for a phone', () => {
    const { chip, panel, settle } = create();
    TestBed.inject(WorkspacePhoneService).requestPicker();
    settle();
    expect(panel()).not.toBeNull();
    expect(chip().getAttribute('aria-expanded')).toBe('true');
  });

  it('is a 44px target and announces its state politely', () => {
    const { chip, el } = create();
    expect(chip().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
    const status = el.querySelector('[role="status"]')!;
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.textContent).toContain('No phone');
  });

  it('only recovers the person’s own phone: it never restarts adb or changes the shared device', () => {
    fakes.relay.state.set({ status: 'dropped', serial: null, sessionId: null, error: 'x' });
    fakes.system.connectedDevices.set([phone()]);
    const { chip, buttons, settle } = create();
    chip().click();
    settle();
    buttons()[0].click(); // pick a phone
    settle();
    chip().click();
    settle();
    buttons().find((b) => b.textContent!.includes('Connect from this browser'))!.click(); // reconnect
    expect(fakes.relay.connect).toHaveBeenCalled();
    expect(fakes.system.restartAdb).not.toHaveBeenCalled();
    expect(fakes.system.selectDevice).not.toHaveBeenCalled();
    expect(fakes.system.useLocalAdbServer).not.toHaveBeenCalled();
  });

  it('says Connecting… while the chooser is open and Attaching… after it (OCR F3)', () => {
    fakes.relay.state.set({ status: 'connecting', serial: null, sessionId: null, error: null });
    const { chip, settle } = create();
    expect(chip().textContent).toContain('Connecting…');
    fakes.relay.attaching.set(true);
    settle();
    expect(chip().textContent).toContain('Attaching…');
  });

  it('shows "Connected in another tab" and Use here takes the phone over, then connects (OCR F3)', async () => {
    fakes.relay.heldInAnotherTab.set({ serial: '127.0.0.1:41003' });
    const { chip, buttons, settle } = create();
    expect(chip().textContent).toContain('Connected in another tab');
    chip().click();
    settle();
    const labels = buttons().map((b) => b.textContent!);
    expect(labels.some((l) => l.includes('Connect from this browser'))).toBeFalse();
    buttons().find((b) => b.textContent!.includes('Use here'))!.click();
    await Promise.resolve();
    await Promise.resolve();
    expect(fakes.tabs.requestRelease).toHaveBeenCalled();
    expect(fakes.relay.connect).toHaveBeenCalled();
  });

  it('never offers a run target for a phone another tab holds', () => {
    fakes.relay.heldInAnotherTab.set({ serial: '127.0.0.1:41003' });
    fakes.system.connectedDevices.set([phone({ serial: '127.0.0.1:41003' })]);
    fakes.system.selectedRunTarget.set('127.0.0.1:41003');
    create();
    expect(TestBed.inject(WorkspacePhoneService).target()).toBeNull();
  });

  describe('viewing history while a run is active (OCR/verifier F4)', () => {
    const OWN = '127.0.0.1:41003';
    beforeEach(() => {
      fakes.relay.state.set({ status: 'connected', serial: OWN, sessionId: 'b', error: null });
      fakes.system.connectedDevices.set([phone({ serial: OWN })]);
      runningOn(OWN);
      viewing('completed'); // a finished run from history is on screen; the live run goes on
    });

    it('still asks before disconnecting the phone a live run is using', () => {
      const { chip, buttons, settle } = create();
      chip().click();
      settle();
      buttons().find((b) => b.textContent!.trim() === 'Disconnect')!.click();
      settle();
      expect(fakes.relay.disconnect).not.toHaveBeenCalled();
      expect(buttons().map((b) => b.textContent!.trim())).toContain('Disconnect and stop');
    });

    it('keeps the phone list locked and says the phone is in use', () => {
      fakes.system.connectedDevices.update((all) => [...all, phone({ serial: 'B', model: 'Pixel 8' })]);
      const { el, chip, buttons, settle } = create();
      chip().click();
      settle();
      expect(buttons().filter((b) => b.classList.contains('option')).every((b) => b.disabled)).toBeTrue();
      expect(el.querySelector('.panel')!.textContent).toContain('Phone in use by this run.');
    });

    it('does not lock the picker for a run on a phone this person is not using', () => {
      fakes.agent.sessions.set([{ session_id: 'theirs', status: 'running', device_serial: 'someone-elses-phone' }]);
      const { chip, buttons, settle } = create();
      chip().click();
      settle();
      expect(buttons().find((b) => b.classList.contains('option'))!.disabled).toBeFalse();
    });

    it('treats a live run whose phone is not known yet as using this phone', () => {
      fakes.agent.sessions.set([{ session_id: 'fresh', status: 'running', device_serial: null }]);
      const { chip, buttons, settle } = create();
      chip().click();
      settle();
      buttons().find((b) => b.textContent!.trim() === 'Disconnect')!.click();
      settle();
      expect(fakes.relay.disconnect).not.toHaveBeenCalled();
    });

    it('does not ask when the live run is on a different phone than the one this browser holds', () => {
      fakes.agent.sessions.set([{ session_id: 'shared', status: 'running', device_serial: 'emulator-5554' }]);
      fakes.system.selectedRunTarget.set(OWN);
      const { chip, buttons, settle } = create();
      chip().click();
      settle();
      buttons().find((b) => b.textContent!.trim() === 'Disconnect')!.click();
      expect(fakes.relay.disconnect).toHaveBeenCalledTimes(1);
    });
  });
});
