import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { AgentService } from '../../services/agent.service';
import { expectHitBox } from '../../testing/hit-box';
import { LegacyAgentStreamComponent } from './legacy-agent-stream.component';

describe('LegacyAgentStreamComponent', () => {
  const agent = {
    currentSessionId: signal('session'),
    sessions: signal([{ session_id: 'session', initial_goal: 'Verify settings', start_time: 1 }]),
    sessionLogs: signal([
      { type: 'step_recorded', timestamp: 1, data: { action_taken: { action: 'tap' } } },
      { type: 'trace_recorded', timestamp: 1, data: { message: 'Connected' } }
    ]),
    selectSession: jasmine.createSpy('selectSession'),
    fetchSessions: jasmine.createSpy('fetchSessions')
  };

  function create() {
    TestBed.configureTestingModule({ providers: [{ provide: AgentService, useValue: agent }] });
    const fixture = TestBed.createComponent(LegacyAgentStreamComponent);
    fixture.nativeElement.style.width = '390px';
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('gives the session picker, refresh and every disclosure a 44px target', () => {
    for (const control of create().querySelectorAll<HTMLElement>('select, button, summary')) {
      expectHitBox(control);
    }
  });

  it('keeps structured and raw disclosures full width in a narrow stream', () => {
    for (const summary of create().querySelectorAll<HTMLElement>('summary')) {
      expect(summary.getBoundingClientRect().width).toBeGreaterThan(250);
      expect(summary.getBoundingClientRect().height).toBeLessThanOrEqual(80);
      const details = summary.parentElement as HTMLDetailsElement;
      const wasOpen = details.open;
      summary.click();
      expect(details.open).toBe(!wasOpen);
    }
  });

  it('selects a session and refreshes through the existing service', () => {
    const element = create();
    element.querySelector('select')!.dispatchEvent(new Event('change'));
    element.querySelector('button')!.click();
    expect(agent.selectSession).toHaveBeenCalledWith('session', true);
    expect(agent.fetchSessions).toHaveBeenCalled();
  });
});
