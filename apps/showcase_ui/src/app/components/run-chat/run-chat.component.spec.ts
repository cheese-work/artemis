import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, Subject, throwError } from 'rxjs';
import { RunChatComponent } from './run-chat.component';
import { RunsService } from '../../services/runs.service';
import { ChatSession, RunSummary } from '../../core/models/run.model';
import { CUSTOM_ELEMENTS_SCHEMA } from '@angular/core';
import { ShellLayoutService } from '../../services/shell-layout.service';
import { signal } from '@angular/core';
import { SessionSidebarComponent } from '../session-sidebar/session-sidebar.component';

describe('RunChatComponent', () => {
  const thread: ChatSession = {
    session_id: 'root', lease_pid: null, idle_until: null,
    turns: [
      { turn_id: 'root', session_id: 'root', number: 1, status: 'completed', created_at: 1 },
      { turn_id: 'follow-up', session_id: 'root', number: 2, status: 'paused', created_at: 2 }
    ],
    messages: [
      { sequence: 1, turn_id: 'root', session_id: 'root', role: 'human', kind: 'message', content: 'Check login', client_id: 'one', reply_to: null, acknowledged_at: 1, created_at: 1 },
      { sequence: 2, turn_id: 'follow-up', session_id: 'root', role: 'agent', kind: 'message', content: 'Proposed plan: check logout', client_id: null, reply_to: null, acknowledged_at: null, created_at: 2 }
    ]
  };
  let api: jasmine.SpyObj<RunsService>;

  it('keeps the timeline and composer visible while restarting polling', fakeAsync(() => {
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges();
    tick(0);
    const previous = fixture.componentInstance.chat();
    const pending = new Subject<RunSummary>();
    api.get.and.returnValue(pending);
    fixture.componentInstance.control('continue');
    fixture.detectChanges();
    expect(fixture.componentInstance.chat()).toBe(previous);
    expect(fixture.componentInstance.rootRun()).not.toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Loading chat session');
    fixture.destroy();
  }));

  beforeEach(() => {
    api = jasmine.createSpyObj<RunsService>('RunsService', ['get', 'chat', 'sendChat', 'controlChat', 'renameChat', 'sendInput']);
    api.get.and.returnValue(of({ session_id: 'root', prompt: 'Check login', read_only: false } as RunSummary));
    api.chat.and.returnValue(of(thread));
    api.sendChat.and.returnValue(of(thread.messages[0]));
    api.sendInput.and.returnValue(of({ accepted: true }));
    api.controlChat.and.returnValue(of(thread.messages[0]));
    api.renameChat.and.returnValue(of({ ...thread, title: 'Login checks' }));
    const technicalDetails = signal(false);
    TestBed.configureTestingModule({ imports: [RunChatComponent], providers: [provideRouter([]), { provide: RunsService, useValue: api },
      { provide: ShellLayoutService, useValue: { technicalDetails, setTechnicalDetails: (enabled: boolean) => technicalDetails.set(enabled) } }
    ] })
      .overrideComponent(RunChatComponent, {
        remove: { imports: [SessionSidebarComponent] },
        add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
      });
  });

  function pendingInput(): ChatSession {
    return { ...thread, messages: [...thread.messages, {
      ...thread.messages[1], sequence: 3, kind: 'input_request', content: 'Enter password',
      payload: { request_id: 'request-1', state: 'pending', secret: true, expires_at: Date.now() / 1000 + 180 }
    }] };
  }

  it('uses a masked field and sends untrimmed input only to the private endpoint', fakeAsync(() => {
    api.chat.and.returnValue(of(pendingInput()));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    const input = fixture.nativeElement.querySelector('#session-input');
    expect(input.type).toBe('password');
    expect(input.autocomplete).toBe('off');
    expect(fixture.nativeElement.querySelector('#session-message')).toBeNull();
    fixture.componentInstance.secretDraft = '  PRIVATE-SENTINEL  ';
    fixture.componentInstance.draft = 'Do not send';
    fixture.componentInstance.send();
    fixture.componentInstance.sendInput();
    expect(api.sendInput).toHaveBeenCalledWith('root', 'request-1', '  PRIVATE-SENTINEL  ');
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(fixture.componentInstance.secretDraft).toBe('');
    expect(JSON.stringify(fixture.componentInstance.chat())).not.toContain('PRIVATE-SENTINEL');
    fixture.destroy();
  }));

  it('preserves input across polls and clears it on a new request or session', fakeAsync(() => {
    const pending = pendingInput();
    api.chat.and.returnValue(of(pending));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    fixture.componentInstance.secretDraft = 'PRIVATE-SENTINEL';
    tick(1000); fixture.detectChanges();
    expect(fixture.componentInstance.secretDraft).toBe('PRIVATE-SENTINEL');
    fixture.componentInstance.chat.set({ ...pending, messages: pending.messages.map(message => message.kind === 'input_request' ? { ...message, payload: { ...message.payload, request_id: 'request-2' } } : message) });
    fixture.detectChanges();
    expect(fixture.componentInstance.secretDraft).toBe('');
    fixture.componentInstance.secretDraft = 'PRIVATE-SENTINEL';
    fixture.componentRef.setInput('runId', 'other'); fixture.detectChanges();
    expect(fixture.componentInstance.secretDraft).toBe('');
    fixture.destroy();
  }));

  it('does not retain or echo a rejected input and prevents shared-view submission', fakeAsync(() => {
    api.chat.and.returnValue(of(pendingInput()));
    api.sendInput.and.returnValue(throwError(() => new Error('PRIVATE-SENTINEL')));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    fixture.componentInstance.secretDraft = 'PRIVATE-SENTINEL';
    fixture.componentInstance.sendInput();
    expect(fixture.componentInstance.secretDraft).toBe('');
    expect(fixture.componentInstance.error()).not.toContain('PRIVATE-SENTINEL');
    api.sendInput.calls.reset();
    fixture.componentRef.setInput('readOnly', true); fixture.detectChanges();
    fixture.componentInstance.secretDraft = 'PRIVATE-SENTINEL';
    fixture.componentInstance.sendInput();
    expect(api.sendInput).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('#session-input')).toBeNull();
    fixture.destroy();
  }));

  function render() {
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges();
    tick(0);
    fixture.detectChanges();
    return fixture;
  }

  it('renders thin turn boundaries in one timeline without a nested run page', fakeAsync(() => {
    const fixture = render();
    expect(fixture.nativeElement.querySelectorAll('.turn-divider').length).toBe(2);
    expect(fixture.nativeElement.querySelectorAll('.flat-timeline').length).toBe(1);
    expect(fixture.nativeElement.querySelector('app-run-view, .chat-turn, .turn-evidence')).toBeNull();
    expect(fixture.componentInstance.messages('root').map(message => message.content)).toEqual(['Check login']);
    expect(fixture.componentInstance.messages('follow-up').map(message => message.content)).toEqual(['Proposed plan: check logout']);
    expect(fixture.nativeElement.textContent).toContain('Continue');
    fixture.destroy();
  }));

  it('sends to the same session rather than creating a new run', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.draft = 'Check logout';
    fixture.componentInstance.send();
    expect(api.sendChat).toHaveBeenCalledWith('root', 'Check logout', jasmine.any(String));
    fixture.componentInstance.control('continue');
    expect(api.controlChat).toHaveBeenCalledWith('root', 'continue', jasmine.any(String));
    fixture.destroy();
  }));

  it('keeps step data inside its own flat timeline row', fakeAsync(() => {
    const fixture = render();
    const steps = [{ step_id: 'step-2', step_number: 1, session_id: 'follow-up', timestamp: 2 }];
    fixture.componentRef.setInput('mode', 'live');
    fixture.componentRef.setInput('liveSession', { session_id: 'follow-up', initial_goal: 'Check logout', start_time: 2 });
    fixture.componentRef.setInput('liveSteps', steps);
    fixture.componentInstance.chat.set({ ...thread, timeline: [
      ...thread.messages,
      { ...thread.messages[1], sequence: 3, kind: 'step', content: 'Open settings', payload: steps[0] }
    ] });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('[data-event-kind="step"]').length).toBe(1);
    expect(fixture.nativeElement.querySelector('app-run-view')).toBeNull();
    expect(fixture.componentInstance.timeline().at(-1)?.turn_id).toBe('follow-up');
    fixture.destroy();
  }));

  it('retains the draft and client ID when retrying a failed send', fakeAsync(() => {
    const fixture = render();
    api.sendChat.and.returnValue(throwError(() => new HttpErrorResponse({ status: 503 })));
    fixture.componentInstance.draft = 'Correction';
    fixture.componentInstance.send();
    const retryId = api.sendChat.calls.mostRecent().args[2];
    fixture.componentInstance.send();
    expect(api.sendChat.calls.mostRecent().args[2]).toBe(retryId);
    expect(fixture.componentInstance.draft).toBe('Correction');
    fixture.destroy();
  }));

  it('renames a visible shared session without enabling message or device writes', fakeAsync(() => {
    const fixture = render();
    fixture.componentRef.setInput('readOnly', true);
    fixture.detectChanges();
    fixture.nativeElement.querySelector('.title-button').click();
    fixture.detectChanges();
    const input = fixture.nativeElement.querySelector('input[aria-label="Session title"]');
    expect(input.maxLength).toBe(120);
    fixture.componentInstance.titleDraft = 'Login checks';
    fixture.nativeElement.querySelector('.rename-form').dispatchEvent(new Event('submit'));
    fixture.detectChanges();
    expect(api.renameChat).toHaveBeenCalledWith('root', 'Login checks');
    expect(fixture.componentInstance.title()).toBe('Login checks');
    expect(fixture.nativeElement.querySelector('textarea')).toBeNull();
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(api.controlChat).not.toHaveBeenCalled();
    fixture.destroy();
  }));

  it('cancels rename with Escape and keeps the current title', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.beginRename();
    fixture.detectChanges();
    fixture.nativeElement.querySelector('input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    fixture.detectChanges();
    expect(fixture.componentInstance.editingTitle()).toBeFalse();
    expect(api.renameChat).not.toHaveBeenCalled();
    fixture.destroy();
  }));

  it('collapses the right sidebar without adding a second title or New session button', fakeAsync(() => {
    const fixture = render();
    expect(fixture.nativeElement.querySelectorAll('h1').length).toBe(1);
    expect(fixture.nativeElement.querySelector('app-session-sidebar')).not.toBeNull();
    fixture.nativeElement.querySelector('[aria-controls="session-sidebar"]').click();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-session-sidebar')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('New session');
    fixture.destroy();
  }));

  it('blocks writes for shared sessions and explicit review-only views', fakeAsync(() => {
    const fixture = render();
    fixture.componentRef.setInput('readOnly', true);
    fixture.detectChanges();
    fixture.componentInstance.draft = 'Do not send';
    fixture.componentInstance.send();
    fixture.componentInstance.control('pause');
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(api.controlChat).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('textarea')).toBeNull();
    fixture.destroy();
  }));

  it('changes technical visibility without changing stored data and keeps the verdict last', fakeAsync(() => {
    const fixture = render();
    const completed: ChatSession = { ...thread, turns: [{ ...thread.turns[0], status: 'completed' }], timeline: [
      thread.messages[0],
      { ...thread.messages[0], role: 'agent', sequence: 3, kind: 'plan', content: 'Open settings' },
      { ...thread.messages[0], role: 'agent', sequence: 4, kind: 'thinking', content: 'Find the control\nMore detail' },
      { ...thread.messages[0], role: 'agent', sequence: 5, kind: 'step', content: 'Tap settings', payload: { step_id: 'step-1', pre_image_name: 'shot.png', action_taken: { x: 42 } } },
      { ...thread.messages[0], role: 'agent', sequence: 6, kind: 'input_request', content: 'Enter username' }
    ], verdict: { status: 'completed', outcome: { phase: 'outcome', tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 }, last_findings: ['Login works'] }, report: 'Raw report', details: { error: 'stack' } } };
    fixture.componentInstance.chat.set(completed);
    const stored = JSON.stringify(completed);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-event-kind="thinking"]')).toBeNull();
    expect(fixture.nativeElement.querySelector('.action-detail')).toBeNull();
    expect(fixture.nativeElement.textContent).not.toContain('Raw report');
    const step = fixture.nativeElement.querySelector('.step-row details');
    expect(step.open).toBeFalse();
    fixture.nativeElement.querySelector('.step-row summary').click();
    expect(step.open).toBeTrue();
    expect(fixture.nativeElement.querySelector('.step-screenshot').src).toContain('/images/shot.png');
    const toggle = fixture.nativeElement.querySelector('[aria-label="Technical details"]');
    toggle.checked = true;
    toggle.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('details.thinking').open).toBeFalse();
    expect(fixture.nativeElement.querySelector('.action-detail').textContent).toContain('42');
    expect(fixture.nativeElement.textContent).toContain('Raw report');
    expect(fixture.nativeElement.querySelector('.flat-timeline').lastElementChild.classList).toContain('verdict-card');
    expect(JSON.stringify(fixture.componentInstance.chat())).toBe(stored);
    fixture.nativeElement.querySelector('.step-screenshot').dispatchEvent(new Event('error'));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.step-row').textContent).toContain('Screenshot unavailable');
    fixture.destroy();
  }));

  it('follows new events only while the reader stays near the end', fakeAsync(() => {
    const fixture = render();
    const history = fixture.nativeElement.querySelector('.chat-history');
    Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(history, 'clientHeight', { configurable: true, value: 200 });
    Object.defineProperty(history, 'scrollTop', { configurable: true, writable: true, value: 0 });
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(1000);
    history.scrollTop = 100;
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(100);
    history.scrollTop = 800;
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(1000);
    fixture.destroy();
  }));
});
