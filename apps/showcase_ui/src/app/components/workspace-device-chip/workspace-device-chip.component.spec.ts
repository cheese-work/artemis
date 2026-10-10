import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { phone, phoneFakes } from '../../testing/phone-fakes';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { WorkspaceDeviceChipComponent } from './workspace-device-chip.component';
import { expectHitBox } from '../../testing/hit-box';

describe('WorkspaceDeviceChipComponent', () => {
  let fakes: ReturnType<typeof phoneFakes>;

  function create() {
    TestBed.configureTestingModule({
      providers: [...fakes.providers, provideRouter([{ path: 'setup', children: [] }])]
    });
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

  it('shows the sidebar device label, mono serial, running state and queue count', () => {
    fakes.system.connectedDevices.set([phone()]);
    fakes.system.selectedRunTarget.set(phone().serial);
    runningOn(phone().serial);
    fakes.agent.sessions.update(all => [...all, { session_id: 'queued', status: 'pending', device_serial: phone().serial }]);
    const { el, chip, settle } = create();
    expect(el.querySelector('.phone-card-label')?.textContent).toBe('Pixel 6 · Phone');
    expect(el.querySelector('.phone-card-serial')?.textContent).toBe(phone().serial);
    expect(el.querySelector('.phone-card-state')?.textContent).toContain('Running');
    expect(el.querySelector('.phone-card-state')?.textContent).toContain('1 queued');
    const header = el.querySelector<HTMLElement>('.phone-card-header')!;
