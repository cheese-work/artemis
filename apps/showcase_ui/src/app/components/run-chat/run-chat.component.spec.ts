import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { RunChatComponent } from './run-chat.component';
import { RunsService } from '../../services/runs.service';
import { ChatSession, RunSummary } from '../../core/models/run.model';

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
    api = jasmine.createSpyObj<RunsService>('RunsService', ['get', 'chat', 'sendChat', 'controlChat']);
    api.get.and.returnValue(of({ session_id: 'root', prompt: 'Check login', read_only: false } as RunSummary));
    api.chat.and.returnValue(of(thread));
    api.sendChat.and.returnValue(of(thread.messages[0]));
    api.controlChat.and.returnValue(of(thread.messages[0]));
    TestBed.configureTestingModule({ imports: [RunChatComponent], providers: [provideRouter([]), { provide: RunsService, useValue: api }] });
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
