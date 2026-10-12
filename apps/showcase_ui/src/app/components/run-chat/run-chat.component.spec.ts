import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { RunChatComponent } from './run-chat.component';
import { RunsService } from '../../services/runs.service';
import { ChatSession, RunSummary } from '../../core/models/run.model';
import { CUSTOM_ELEMENTS_SCHEMA } from '@angular/core';
import { RunViewComponent } from '../run-view/run-view.component';
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

  beforeEach(() => {
    api = jasmine.createSpyObj<RunsService>('RunsService', ['get', 'chat', 'sendChat', 'controlChat', 'renameChat']);
    api.get.and.returnValue(of({ session_id: 'root', prompt: 'Check login', read_only: false } as RunSummary));
    api.chat.and.returnValue(of(thread));
    api.sendChat.and.returnValue(of(thread.messages[0]));
    api.controlChat.and.returnValue(of(thread.messages[0]));
    api.renameChat.and.returnValue(of({ ...thread, title: 'Login checks' }));
    TestBed.configureTestingModule({ imports: [RunChatComponent], providers: [provideRouter([]), { provide: RunsService, useValue: api }] })
      .overrideComponent(RunChatComponent, {
        remove: { imports: [RunViewComponent, SessionSidebarComponent] },
        add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
      });
  });

  function render() {
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges();
    tick(0);
    fixture.detectChanges();
    return fixture;
  }

  it('renders two turns and keeps each evidence panel collapsed', fakeAsync(() => {
    const fixture = render();
    expect(fixture.nativeElement.querySelectorAll('.chat-turn').length).toBe(2);
    expect(fixture.nativeElement.querySelectorAll('.turn-evidence[open]').length).toBe(0);
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

  it('keeps live steps out of earlier turns when evidence is expanded', fakeAsync(() => {
    const fixture = render();
    const steps = [{ step_id: 'step-2', step_number: 1, session_id: 'follow-up', timestamp: 2 }];
    fixture.componentRef.setInput('mode', 'live');
    fixture.componentRef.setInput('liveSession', { session_id: 'follow-up', initial_goal: 'Check logout', start_time: 2 });
    fixture.componentRef.setInput('liveSteps', steps);
    const disclosures = fixture.nativeElement.querySelectorAll('details') as NodeListOf<HTMLDetailsElement>;
    disclosures.forEach(disclosure => { disclosure.open = true; disclosure.dispatchEvent(new Event('toggle')); });
    fixture.detectChanges();
    const viewers = fixture.nativeElement.querySelectorAll('app-run-view');
    expect(viewers[0].mode).toBe('review');
    expect(viewers[0].runId).toBe('root');
    expect(viewers[0].liveSteps).toEqual([]);
    expect(viewers[1].mode).toBe('live');
    expect(viewers[1].runId).toBe('follow-up');
    expect(viewers[1].liveSteps).toEqual(steps);
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
});
