import { TestBed } from '@angular/core/testing';
import { phone, phoneFakes } from '../../testing/phone-fakes';
import { InterruptedBannerComponent } from './interrupted-banner.component';

describe('InterruptedBannerComponent', () => {
  let fakes: ReturnType<typeof phoneFakes>;

  function create(lastStep: number | null = 3) {
    TestBed.configureTestingModule({ providers: fakes.providers });
    const fixture = TestBed.createComponent(InterruptedBannerComponent);
    fixture.componentRef.setInput('lastStep', lastStep);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const settle = () => { fixture.detectChanges(); TestBed.tick(); fixture.detectChanges(); };
    return { fixture, el, settle };
  }

  beforeEach(() => {
    fakes = phoneFakes();
    fakes.agent.currentSession.set({ status: 'interrupted', initial_goal: 'Open Settings', interrupt_reason: 'device_offline' });
  });

  it('stays out of the way unless the run on screen was interrupted', () => {
    fakes.agent.currentSession.set({ status: 'completed' });
    expect(create().el.querySelector('.banner')).toBeNull();
  });

  it('names the step, says reconnecting does not resume, and gives the reason', () => {
    const { el } = create();
    const banner = el.querySelector('.banner')!;
    expect(banner.getAttribute('role')).toBe('status');
    expect(banner.textContent).toContain('Run interrupted at step 3. Recording is partial. Reconnecting will not resume this run.');
    expect(banner.textContent).toContain('The phone went offline.');
  });

  it('offers Reconnect phone first; it only reconnects, never resumes', () => {
    const { el } = create();
    const button = el.querySelector<HTMLButtonElement>('button')!;
    expect(button.textContent).toContain('Reconnect phone');
    button.click();
    expect(fakes.relay.connect).toHaveBeenCalled();
    expect(fakes.agent.resumeTask).not.toHaveBeenCalled();
  });

  it('offers a new run with the same prompt once a phone is connected again', () => {
    const { fixture, el, settle } = create();
    const started: string[] = [];
    fixture.componentInstance.startNewRun.subscribe((prompt) => started.push(prompt));
    fakes.relay.state.set({ status: 'connected', serial: '127.0.0.1:41003', sessionId: 'b', error: null });
    fakes.system.connectedDevices.set([phone({ serial: '127.0.0.1:41003' })]);
    settle();
    const button = el.querySelector<HTMLButtonElement>('button')!;
    expect(button.textContent).toContain('Start new run with this prompt');
    button.click();
    expect(started).toEqual(['Open Settings']);
    expect(fakes.agent.resumeTask).not.toHaveBeenCalled();
  });

  it('says "before the first step" when no step ran, and has a safe sentence for an unknown reason', () => {
    fakes.agent.currentSession.set({ status: 'interrupted', initial_goal: 'x', interrupt_reason: 'something_new' });
    const text = create(null).el.textContent!;
    expect(text).toContain('Run interrupted before the first step.');
    expect(text).toContain('The run stopped before it finished.');
  });

  it('has a 44px Reconnect target', () => {
    const { el } = create();
    expect(el.querySelector('button')!.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  });
});
