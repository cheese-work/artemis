import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, Subject, throwError } from 'rxjs';
import { RunChatComponent } from './run-chat.component';
import { RunsService } from '../../services/runs.service';
import { ChatSession, ChatTurnResult, RunSummary } from '../../core/models/run.model';
import { CUSTOM_ELEMENTS_SCHEMA } from '@angular/core';
import { ShellLayoutService } from '../../services/shell-layout.service';
import { signal } from '@angular/core';
import { SessionSidebarComponent } from '../session-sidebar/session-sidebar.component';
import { SystemService } from '../../services/system.service';
import { DeviceInfo } from '../../core/models/system.model';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';

describe('RunChatComponent', () => {
  it('reports deploy drain remaining time and keeps the follow-up draft', fakeAsync(() => {
    api.sendChat.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 503,
      error: { detail: { code: 'server_draining', deadline: Date.now() / 1000 + 75 } }
    })));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0);
    fixture.componentInstance.draft = 'Keep this prompt';
    fixture.componentInstance.send();
    expect(fixture.componentInstance.draft).toBe('Keep this prompt');
    expect(fixture.componentInstance.error()).toContain('SmartQA đang cập nhật');
    expect(fixture.componentInstance.error()).toContain('75');
    fixture.destroy();
  }));

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
  const devices = signal<DeviceInfo[]>([]);
  const readiness = signal<object | null>({});
  const pickerOptions = signal<{ serial: string; text: string; usable: boolean; note: string | null }[]>([]);
  let phoneService: { options: typeof pickerOptions; canConnectFromBrowser: ReturnType<typeof signal<boolean>>; connectFromBrowser: jasmine.Spy; choose: jasmine.Spy };

  it('fills the empty-session composer with an upstream sample without sending', () => {
    const fixture = TestBed.createComponent(RunChatComponent);
    const emitted = spyOn(fixture.componentInstance.newRunPrompt, 'emit');
    fixture.detectChanges();
    const button = fixture.nativeElement.querySelector('[data-sample]');
    expect(button).not.toBeNull();
    button.click();
    expect(emitted).toHaveBeenCalledWith(jasmine.stringMatching('Open the notification panel'));
    expect(api.sendChat).not.toHaveBeenCalled();
    expect(api.sendInput).not.toHaveBeenCalled();
    fixture.destroy();
  });

  it('keeps the completed-run tip inside the final verdict and copies it', fakeAsync(() => {
    api.chat.and.returnValue(of({ ...thread, turns: [{ ...thread.turns[0], status: 'completed' }], timeline: thread.messages, verdict: { status: 'completed', outcome: null, details: {}, tip: { state: 'complete', text: 'Specify the expected screen next time.' } } }));
    const clipboard = spyOn(navigator.clipboard, 'writeText').and.returnValue(Promise.resolve());
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    const verdict = fixture.nativeElement.querySelector('.verdict-card');
    expect(verdict).toBe(fixture.nativeElement.querySelector('.flat-timeline').lastElementChild);
    expect(verdict.querySelector('.verdict-tip').textContent).toContain('Specify the expected screen');
    verdict.querySelector('.copy-tip').click(); tick(0);
    expect(clipboard).toHaveBeenCalledOnceWith('Specify the expected screen next time.');
    fixture.destroy();
  }));

  it('renders backend verdicts per turn and places cited failing evidence first', fakeAsync(() => {
    const failingStep: ChatSession['messages'][number] = {
      ...thread.messages[1], sequence: 3, kind: 'step', content: 'Submit sign-in',
      payload: {
        step_id: 'failed-step', step_number: 2, post_image_name: 'failed.png',
        last_execution_result: { expected: 'Account home is visible', actual: 'Invalid credentials banner is visible' }
      }
    };
    const unrelatedStep = {
      ...failingStep, sequence: 4, content: 'Open help',
      payload: { ...failingStep.payload, step_id: 'help-step', step_number: 3 }
    };
    const result = {
      catalog_key: 'fail.app_defect', verdict: 'fail', verification: 'self_reported', category: 'app_defect',
      display_labels: { verification: 'Agent tự báo', category: 'Lỗi ứng dụng' },
      reason: 'App đang test (agent nhận định)',
      action: { text: 'Xem bằng chứng rồi báo bug cho app' }, responsible: 'App đang test (agent nhận định)',
      evidence: { turn_id: 'follow-up', step: 2, screenshot_url: '/api/images/failed.png' },
      turn_index: 2, turn_count: 2, label_state: 'final', source: 'outcome'
    };
    const chat = {
      ...thread,
      turns: [thread.turns[0], { ...thread.turns[1], status: 'completed' }],
      timeline: [...thread.messages, failingStep, unrelatedStep],
      results: [{ ...result, evidence: { ...result.evidence, turn_id: 'root' }, verdict: 'pass', catalog_key: 'pass.verified', reason: 'Đã kiểm' }, result]
    } as any;
    api.chat.and.returnValue(of(chat));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();

    const cards = fixture.nativeElement.querySelectorAll('[data-testid="run-verdict"]');
    expect(cards.length).toBe(2);
    expect(cards[0].textContent).toContain('Đã kiểm');
    expect(cards[1].getAttribute('data-catalog-key')).toBe('fail.app_defect');
    expect(cards[1].textContent).toContain('App đang test (agent nhận định)');
    expect(cards[1].textContent).toContain('Xem bằng chứng rồi báo bug cho app');
    expect(cards[1].textContent).toContain('Kết quả của lượt 2/2');
    expect(cards[1].getAttribute('data-catalog-key')).toBe('fail.app_defect');
    expect(cards[1].textContent).not.toContain('fail.app_defect');
    expect(cards[1].querySelector('.verification').textContent).toContain('Xác minh · Agent tự báo');
    expect(cards[1].querySelector('.category-label').textContent).toBe('Lỗi ứng dụng');
    expect(cards[1].textContent).not.toContain('Verification · self_reported');
    expect(cards[1].querySelector('.verdict-action').textContent).toContain('Hành động ·');
    expect(cards[1].querySelector('.verdict-responsible').textContent).toContain('Phụ trách ·');
    const turnDividers = fixture.nativeElement.querySelectorAll('.turn-divider');
    expect(turnDividers[1].textContent).toContain('Lượt 2');
    expect(turnDividers[1].textContent).not.toContain('Turn');
    const layout = cards[1].querySelector('.verdict-layout');
    expect(layout).not.toBeNull();
    if (layout) {
      expect(layout.children[0].classList).toContain('verdict-rail');
      expect(layout.children[1]).toBe(cards[1].querySelector('[data-testid="run-decisive-evidence"]'));
    }
    expect(getComputedStyle(cards[1].querySelector('.verdict-chip')).fontSize).toBe('32px');
    expect(getComputedStyle(cards[1].querySelector('.verdict-chip')).lineHeight).toBe('40px');

    const selectedEvidence = cards[1].querySelector('[data-testid="selected-step-evidence"]');
    expect(selectedEvidence.querySelector('.evidence-kicker').textContent).toContain('Bước lỗi · 2');
    expect(selectedEvidence.textContent).toContain('Mong đợi');
    expect(selectedEvidence.textContent).toContain('Account home is visible');
    expect(selectedEvidence.textContent).toContain('Thực tế');
    expect(selectedEvidence.textContent).toContain('Invalid credentials banner is visible');
    const evidencePanel = cards[1].querySelector('[data-testid="run-decisive-evidence"]');
    expect(evidencePanel.getAttribute('aria-label')).toBe('Bằng chứng quyết định');
    expect(evidencePanel.querySelector('.other-evidence-steps summary').textContent.trim()).toBe('Các bước khác');
    expect(evidencePanel.querySelector('.evidence-link').textContent.trim()).toBe('Mở bước trong dòng thời gian');
    expect(evidencePanel.querySelector('.evidence-image').getAttribute('aria-label')).toBe('Mở ảnh chụp bằng chứng quyết định');
    expect(evidencePanel.querySelector('.evidence-image img').getAttribute('alt')).toBe('Ảnh chụp bằng chứng quyết định');
    expect(evidencePanel.firstElementChild).toBe(selectedEvidence);
    expect(selectedEvidence.compareDocumentPosition(evidencePanel.querySelector('[data-testid="evidence-index"]'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(evidencePanel.querySelector('[data-testid="evidence-index"]').compareDocumentPosition(evidencePanel.querySelector('[data-testid="run-evidence-screenshot"]'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(evidencePanel.querySelector('[data-testid="run-evidence-screenshot"]').compareDocumentPosition(evidencePanel.querySelector('.other-evidence-steps'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(fixture.nativeElement.querySelectorAll('.chat-messages[aria-live="polite"]').length).toBe(1);
    expect(fixture.nativeElement.querySelectorAll('[data-testid="run-verdict-announcement"][aria-live="polite"]').length).toBe(1);
    expect(cards[1].getAttribute('aria-live')).toBe('off');
    const citedStep = fixture.nativeElement.querySelector('[data-turn-id="follow-up"][data-step-number="2"] details') as HTMLDetailsElement;
    expect(citedStep.open).toBeFalse();
    evidencePanel.querySelector('[data-testid="run-decisive-evidence-link"]').click();
    fixture.detectChanges();
    expect(citedStep.open).toBeTrue();
    expect(document.activeElement).toBe(citedStep.querySelector('summary'));
    fixture.destroy();
  }));

  it('contains a long results note inside the phone-width chat viewport', fakeAsync(() => {
    const failingStep: ChatSession['messages'][number] = {
      ...thread.messages[1], sequence: 3, kind: 'step', content: 'Submit sign-in',
      payload: { step_number: 2, last_execution_result: { expected: 'Account home is visible', actual: 'Invalid credentials banner is visible' } }
    };
    const result: ChatTurnResult = {
      catalog_key: 'fail.app_defect', verdict: 'fail', verification: 'self_reported', category: 'app_defect',
      display_labels: { verification: 'Agent tự báo', category: 'Lỗi ứng dụng' },
      reason: 'The result note contains a long unbroken identifier.',
      action: { text: 'Review the evidence' }, responsible: 'App under test',
      evidence: { turn_id: 'follow-up', step: 2, screenshot_url: null },
      turn_index: 2, turn_count: 2, label_state: 'final', source: 'outcome',
      results_note: { text: `evidence-${'x'.repeat(180)}`, modified_at: 2, truncated: false }
    };
    api.chat.and.returnValue(of({
      ...thread,
      turns: [thread.turns[0], { ...thread.turns[1], status: 'completed' }],
      timeline: [...thread.messages, failingStep],
      results: [result]
    }));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.nativeElement.style.width = '390px';
    fixture.nativeElement.style.height = '700px';
    fixture.detectChanges(); tick(0); fixture.detectChanges();

    const host = fixture.nativeElement.getBoundingClientRect();
    const evidence = fixture.nativeElement.querySelector('[data-testid="selected-step-evidence"]').getBoundingClientRect();
    expect(evidence.right).toBeLessThanOrEqual(host.right);
    fixture.destroy();
  }));

  it('renders each backend verdict state with a matching polite announcement', fakeAsync(() => {
    const states = [
      { turnStatus: 'completed', labelState: 'final', verdict: 'pass', expected: 'Pass' },
      { turnStatus: 'failed', labelState: 'final', verdict: 'fail', expected: 'Fail' },
      { turnStatus: 'interrupted', labelState: 'final', verdict: 'blocked', expected: 'Blocked' },
      { turnStatus: 'cancelled', labelState: 'cancelled', verdict: 'cancelled', expected: 'Đã huỷ' },
      { turnStatus: 'completed', labelState: 'legacy', verdict: 'blocked', expected: 'Run cũ — chưa có verdict' }
    ];
    for (const state of states) {
      const fixture = render();
      fixture.componentInstance.chat.set({
        ...thread,
        turns: [{ ...thread.turns[0], status: state.turnStatus }],
        results: [{
          catalog_key: 'fixture.verdict', verdict: state.verdict, verification: 'self_reported', category: 'unknown',
          reason: 'Fixture reason', action: { text: 'Fixture action' }, responsible: 'Fixture owner',
          evidence: { turn_id: 'root', step: null, screenshot_url: null }, turn_index: 1, turn_count: 1,
          label_state: state.labelState, source: 'outcome'
        } as any]
      });
      fixture.detectChanges();
      const visibleLabel = fixture.nativeElement.querySelector('.verdict-chip').textContent.trim();
      const announcement = fixture.nativeElement.querySelector('[data-testid="run-verdict-announcement"]').textContent.trim();
      expect(visibleLabel).toBe(state.expected);
      expect(announcement).toBe(visibleLabel);
      fixture.destroy();
    }
  }));

  it('keeps pending labels polling until the final backend verdict arrives', fakeAsync(() => {
    const pending: ChatSession = {
      ...thread, turns: [{ ...thread.turns[0], status: 'completed' }],
      results: [{
        catalog_key: 'pending', verdict: 'blocked', verification: 'pending', category: null, reason: 'Đang xác định kết quả',
        action: { text: '' }, responsible: '', evidence: { turn_id: 'root', step: null, screenshot_url: null },
        turn_index: 1, turn_count: 1, label_state: 'pending', source: 'outcome'
      }]
    };
    const finalResult = { ...pending.results![0], catalog_key: 'pass.verified', verdict: 'pass', reason: 'Đã kiểm', label_state: 'final' as const };
    api.chat.and.returnValues(of(pending), of({ ...pending, results: [finalResult] }));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.verdict-chip').textContent.trim()).toBe('Đã kết thúc · Đang xác định kết quả');
    tick(1000); fixture.detectChanges();
    const visibleLabel = fixture.nativeElement.querySelector('.verdict-chip').textContent.trim();
    expect(visibleLabel).toBe('Pass');
    expect(fixture.nativeElement.querySelector('[data-testid="run-verdict-announcement"]').textContent.trim()).toBe(visibleLabel);
    fixture.destroy();
  }));

  it('keeps chat messages polite and exposes one verdict announcement', fakeAsync(() => {
    const fixture = render();
    expect(fixture.nativeElement.querySelector('.chat-messages').getAttribute('aria-live')).toBe('polite');
    expect(fixture.nativeElement.querySelectorAll('[data-testid="run-verdict-announcement"][aria-live="polite"]').length).toBe(1);
    fixture.destroy();
  }));

  it('starts the pending poll cap only after a long-running turn becomes pending', fakeAsync(() => {
    const pendingResult = {
      catalog_key: 'blocked.unknown', verdict: 'blocked', verification: 'none', category: 'unknown',
      display_labels: { verification: 'Chưa xác minh', category: 'Chưa xác định' },
      reason: 'Đang xác định kết quả', action: { text: '' }, responsible: '',
      evidence: { turn_id: 'follow-up', step: null, screenshot_url: null },
      turn_index: 2, turn_count: 2, label_state: 'pending', source: 'outcome'
    } as ChatTurnResult;
    const finalResult = { ...pendingResult, catalog_key: 'fail.app_defect', verdict: 'fail', category: 'app_defect', label_state: 'final' as const };
    const activeChat: ChatSession = { ...thread, turns: [thread.turns[0], { ...thread.turns[1], status: 'running' }], results: [] };
    const pendingChat: ChatSession = { ...thread, turns: [thread.turns[0], { ...thread.turns[1], status: 'completed' }], results: [pendingResult] };
    const finalChat: ChatSession = { ...pendingChat, results: [finalResult] };
    let call = 0;
    api.chat.and.callFake(() => {
      call++;
      return of(call <= 40 ? activeChat : call === 41 ? pendingChat : finalChat);
    });
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); tick(41000); fixture.detectChanges();

    expect(api.chat.calls.count()).toBe(42);
    expect(fixture.componentInstance.latestResultLabel()).toBe('Fail');
    fixture.destroy();
  }));

  it('keeps live polling through a transient failed request', fakeAsync(() => {
    const finalResult = {
      catalog_key: 'fail.app_defect', verdict: 'fail', verification: 'self_reported', category: 'app_defect',
      display_labels: { verification: 'Agent tự báo', category: 'Lỗi ứng dụng' },
      reason: 'Không đăng nhập được', action: { text: 'Kiểm tra bằng chứng' }, responsible: 'Ứng dụng',
      evidence: { turn_id: 'follow-up', step: null, screenshot_url: null },
      turn_index: 2, turn_count: 2, label_state: 'final', source: 'outcome'
    } as ChatTurnResult;
    const activeChat: ChatSession = { ...thread, turns: [thread.turns[0], { ...thread.turns[1], status: 'running' }], results: [] };
    const finalChat: ChatSession = { ...thread, turns: [thread.turns[0], { ...thread.turns[1], status: 'failed' }], results: [finalResult] };
    let call = 0;
    api.chat.and.callFake(() => {
      call++;
      if (call === 3) return throwError(() => new HttpErrorResponse({ status: 502 }));
      return of(call < 5 ? activeChat : finalChat);
    });
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); tick(6000); fixture.detectChanges();

    expect(api.chat.calls.count()).toBeGreaterThan(4);
    expect(fixture.componentInstance.latestResultLabel()).toBe('Fail');
    fixture.destroy();
  }));

  it('copies the blocked turn ID and clears feedback after success', fakeAsync(() => {
    const blockedResult = {
      catalog_key: 'blocked.qa_environment', verdict: 'blocked', verification: 'none', category: 'qa_environment',
      display_labels: { verification: 'Chưa xác minh', category: 'Môi trường QA' },
      reason: 'Điện thoại đang khoá', action: { text: 'Mở khoá điện thoại' }, responsible: 'Môi trường QA',
      evidence: { turn_id: 'follow-up', step: null, screenshot_url: null },
      turn_index: 2, turn_count: 2, label_state: 'final', source: 'ledger'
    } as ChatTurnResult;
    api.chat.and.returnValue(of({ ...thread, turns: thread.turns.map(turn => ({ ...turn, status: 'completed' })), results: [blockedResult] }));
    const clipboard = spyOn(navigator.clipboard, 'writeText').and.returnValue(Promise.resolve());
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.copy-run-id').textContent.trim()).toBe('Sao chép mã lượt chạy');
    expect(fixture.nativeElement.querySelector('.verdict-help').getAttribute('aria-label')).toBe('Trợ giúp về kết quả');
    fixture.nativeElement.querySelector('.copy-run-id').click();
    tick(0); fixture.detectChanges();

    expect(clipboard).toHaveBeenCalledOnceWith('follow-up');
    expect(fixture.nativeElement.querySelectorAll('.copy-notice').length).toBe(1);
    tick(2000); fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('.copy-notice').length).toBe(0);
    fixture.destroy();
  }));

  it('preserves the category-specific help route for the backend result', fakeAsync(() => {
    const fixture = render();
    expect(fixture.componentInstance.helpHref({ category: 'qa_environment' } as ChatTurnResult))
      .toBe('/docs/smartqa-verdicts.html#qa_environment');
    fixture.destroy();
  }));

  it('shows and retries a verdict load error before the first chat loads', fakeAsync(() => {
    const recovered: ChatSession = {
      ...thread, turns: [{ ...thread.turns[0], status: 'completed' }],
      results: [{
        catalog_key: 'pass.verified', verdict: 'pass', verification: 'checked', category: null, reason: 'Đã kiểm',
        action: { text: '' }, responsible: '', evidence: { turn_id: 'root', step: null, screenshot_url: null },
        turn_index: 1, turn_count: 1, label_state: 'final', source: 'outcome'
      }]
    };
    api.chat.and.returnValues(throwError(() => new HttpErrorResponse({ status: 503 })), of(recovered));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-label-state="error"] .verdict-state').textContent.trim())
      .toBe('Không tải được kết quả · Thử lại');
    expect(fixture.nativeElement.querySelector('[data-testid="run-verdict-announcement"]').textContent.trim())
      .toBe('Không tải được kết quả · Thử lại');
    fixture.nativeElement.querySelector('.verdict-retry').click();
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    expect(api.chat.calls.count()).toBe(2);
    expect(fixture.nativeElement.querySelector('.verdict-chip').textContent.trim()).toBe('Pass');
    fixture.destroy();
  }));

  it('polls until the completed-run tip settles and never displays one for incomplete execution', fakeAsync(() => {
    let current: ChatSession = { ...thread, turns: [{ ...thread.turns[0], status: 'completed' }], verdict: { status: 'completed', outcome: null, details: {}, tip: { state: 'generating', text: null } } };
    api.chat.and.callFake(() => of(current));
    const fixture = TestBed.createComponent(RunChatComponent);
    fixture.componentRef.setInput('runId', 'root');
    fixture.detectChanges(); tick(0); fixture.detectChanges();
    current = { ...current, verdict: { ...current.verdict!, tip: { state: 'complete', text: 'Tip' } } };
    tick(1000); fixture.detectChanges();
    const calls = api.chat.calls.count();
    tick(3000); expect(api.chat.calls.count()).toBe(calls);
    for (const status of ['failed', 'interrupted', 'cancelled']) {
      fixture.componentInstance.chat.set({ ...current, verdict: { ...current.verdict!, status } });
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.verdict-tip')).toBeNull();
    }
    fixture.destroy();
  }));

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
    devices.set([]);
    readiness.set({});
    pickerOptions.set([]);
    phoneService = { options: pickerOptions, canConnectFromBrowser: signal(true), connectFromBrowser: jasmine.createSpy('connectFromBrowser').and.resolveTo(), choose: jasmine.createSpy('choose') };
    TestBed.configureTestingModule({ imports: [RunChatComponent], providers: [provideRouter([]), { provide: RunsService, useValue: api },
      { provide: SystemService, useValue: { connectedDevices: devices, readinessReport: readiness } },
      { provide: WorkspacePhoneService, useValue: phoneService },
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

  describe('agent results note under the verdict', () => {
    const verdictWith = (status: string, results_note: object | null): ChatSession => ({
      ...thread, turns: [{ ...thread.turns[0], status }], timeline: thread.messages,
      verdict: { status, outcome: null, details: {}, result: { results_note } as never }
    });
    const note = { text: '| Mục | Trạng thái | Kết quả | Bằng chứng |\n| Phiên 1 | PASS | 12 server | step 4 |', modified_at: 1793000000, truncated: false };

    it('labels a note from an unfinished run as partial with its ICT time', fakeAsync(() => {
      const fixture = render();
      fixture.componentInstance.chat.set(verdictWith('failed', note));
      fixture.detectChanges();
      const block = fixture.nativeElement.querySelector('.verdict-card .results-note');
      expect(block.querySelector('h3').textContent).toContain('Kết quả dở dang — agent ghi lúc 14:33 ICT');
      expect(block.querySelector('pre').textContent).toContain('Phiên 1 | PASS');
      fixture.destroy();
    }));

    it('drops the partial label once the run completed', fakeAsync(() => {
      const fixture = render();
      fixture.componentInstance.chat.set(verdictWith('completed', note));
      fixture.detectChanges();
      const heading = fixture.nativeElement.querySelector('.results-note h3').textContent;
      expect(heading).toContain('Kết quả — agent ghi lúc');
      expect(heading).not.toContain('dở dang');
      fixture.destroy();
    }));

    it('says so when the agent wrote no results', fakeAsync(() => {
      const fixture = render();
      fixture.componentInstance.chat.set(verdictWith('failed', null));
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.verdict-card').textContent).toContain('Agent chưa ghi kết quả nào');
      expect(fixture.nativeElement.querySelector('.results-note')).toBeNull();
      fixture.destroy();
    }));
  });

  it('changes technical visibility without changing stored data and keeps the verdict last', fakeAsync(() => {
    const fixture = render();
    const completed: ChatSession = { ...thread, turns: [{ ...thread.turns[0], status: 'completed' }], timeline: [
      thread.messages[0],
      { ...thread.messages[0], role: 'agent', sequence: 3, kind: 'plan', content: 'Open settings' },
      { ...thread.messages[0], role: 'agent', sequence: 4, kind: 'thinking', content: 'Find the control\nMore detail' },
      { ...thread.messages[0], role: 'agent', sequence: 5, kind: 'step', content: 'Tap settings', payload: { step_id: 'step-1', post_image_name: 'shot.png', action_taken: { x: 42 } } },
      { ...thread.messages[0], role: 'agent', sequence: 6, kind: 'input_request', content: 'Enter username' }
    ], verdict: { status: 'completed', outcome: { phase: 'outcome', tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 }, last_findings: ['Login works'] }, report: 'Raw report', details: { error: 'stack' } } };
    fixture.componentInstance.chat.set(completed);
    const stored = JSON.stringify(completed);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('details.thinking')).not.toBeNull(); // thinking is in the default view
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

  const stepMessage = (over: Record<string, unknown> = {}) => ({
    ...thread.messages[0], role: 'agent', sequence: 5, kind: 'step', content: 'Tap settings',
    payload: { step_id: 'step-1', pre_image_name: 'pre.png', post_image_name: 'post.png', action_taken: { x: 42 } }, ...over
  });
  const withTimeline = (...messages: object[]): ChatSession => ({ ...thread, turns: [{ ...thread.turns[0], status: 'completed' }], timeline: messages as ChatSession['messages'] });

  it('leads a step row with the action, then the post-action screenshot', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage()));
    fixture.detectChanges();
    const summary = fixture.nativeElement.querySelector('.step-row summary') as HTMLElement;
    const label = summary.querySelector('span')!;
    const thumbnail = summary.querySelector('img.step-thumbnail') as HTMLImageElement;
    expect(label.compareDocumentPosition(thumbnail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(thumbnail.src).toContain('/images/post.png');
    fixture.destroy();
  }));

  it('never shows a Before image as the post-action screenshot', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage({ payload: { step_id: 'step-1', pre_image_name: 'before.png', action_taken: { x: 42 } } })));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('img.step-thumbnail')).toBeNull();
    fixture.nativeElement.querySelector('.step-row summary').click();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.step-screenshot')).toBeNull();
    expect(fixture.nativeElement.querySelector('.step-row').textContent).toContain('Screenshot unavailable');
    fixture.destroy();
  }));

  it('wraps a long step label to two lines at 390 px instead of truncating it', fakeAsync(() => {
    const fixture = render();
    fixture.nativeElement.style.width = '390px';
    fixture.componentInstance.sidebarOpen.set(false); // at a real 390 px the sidebar stacks below; keep this window-size independent
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage({ content: 'Tap first result PSY - GANGNAM STYLE official music video' })));
    fixture.detectChanges();
    const label = fixture.nativeElement.querySelector('.step-row summary span') as HTMLElement;
    const lineHeight = parseFloat(getComputedStyle(label).lineHeight) || 16;
    expect(label.getBoundingClientRect().height).toBeGreaterThan(lineHeight * 1.5);
    expect(label.scrollHeight).toBeLessThanOrEqual(label.clientHeight);
    fixture.destroy();
  }));

  it('shows the thinking item in the default view, dimmed and collapsed', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0], { ...thread.messages[0], role: 'agent', sequence: 4, kind: 'thinking', content: 'Find the control\nMore detail' }));
    fixture.detectChanges();
    const thinking = fixture.nativeElement.querySelector('details.thinking') as HTMLDetailsElement;
    expect(thinking.open).toBeFalse();
    expect(getComputedStyle(thinking).color).toBe('rgb(107, 107, 115)'); // --color-text-faint
    fixture.destroy();
  }));

  it('shows skeleton rows while the session loads', fakeAsync(() => {
    api.chat.and.returnValue(new Subject<ChatSession>());
    const fixture = render();
    expect(fixture.nativeElement.querySelectorAll('.step-skeleton').length).toBeGreaterThan(0);
    expect(fixture.nativeElement.querySelector('[role="status"]').textContent).toContain('Loading chat session');
    fixture.destroy();
  }));

  it('says what is missing when a session has no steps, and drops the note once one exists', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0]));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.timeline-empty').textContent).toContain('No steps were recorded.');
    fixture.componentInstance.chat.set({ ...withTimeline(thread.messages[0]), turns: [{ ...thread.turns[0], status: 'running' }] });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.timeline-empty').textContent).toContain('Waiting for the first step.');
    fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage()));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.timeline-empty')).toBeNull();
    fixture.destroy();
  }));

  it('stops following on a one-pixel scroll-up and resumes only at the bottom', fakeAsync(() => {
    const fixture = render();
    const history = fixture.nativeElement.querySelector('.chat-history');
    Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(history, 'clientHeight', { configurable: true, value: 200 });
    Object.defineProperty(history, 'scrollTop', { configurable: true, writable: true, value: 0 });
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(1000);
    history.scrollTop = 799;
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(799);
    fixture.componentInstance.chat.set({ ...thread, timeline: [...thread.messages] });
    fixture.detectChanges();
    expect(history.scrollTop).toBe(799);
    history.scrollTop = 800;
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(1000);
    fixture.destroy();
  }));

  it('resumes following at a fractional bottom on a scaled display', fakeAsync(() => {
    const fixture = render();
    const history = fixture.nativeElement.querySelector('.chat-history');
    Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(history, 'clientHeight', { configurable: true, value: 200 });
    Object.defineProperty(history, 'scrollTop', { configurable: true, writable: true, value: 799 });
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(799);
    history.scrollTop = 799.5;
    fixture.componentInstance.trackScroll();
    fixture.componentInstance.scrollToLatest();
    expect(history.scrollTop).toBe(1000);
    fixture.destroy();
  }));

  it('does not replace chat or scroll when a poll returns unchanged content', fakeAsync(() => {
    const fixture = render();
    const chat = fixture.componentInstance.chat();
    const scroll = spyOn(fixture.componentInstance, 'scrollToLatest');
    api.chat.and.returnValue(of(JSON.parse(JSON.stringify(thread))));
    tick(1000); fixture.detectChanges();
    expect(api.chat).toHaveBeenCalledTimes(2);
    expect(fixture.componentInstance.chat()).toBe(chat);
    expect(scroll).not.toHaveBeenCalled();
    fixture.destroy();
  }));

  it('does not auto-scroll for composer-only renders', fakeAsync(() => {
    const fixture = render();
    const scroll = spyOn(fixture.componentInstance, 'scrollToLatest');
    fixture.componentInstance.sending.set(true);
    fixture.detectChanges();
    expect(scroll).not.toHaveBeenCalled();
    fixture.destroy();
  }));

  it('anchors a final verdict at the top of compact history while following latest', fakeAsync(() => {
    const fixture = render();
    const history = fixture.nativeElement.querySelector('.chat-history') as HTMLElement;
    const verdict = document.createElement('li');
    verdict.dataset['testid'] = 'run-verdict';
    verdict.dataset['labelState'] = 'final';
    history.appendChild(verdict);
    Object.defineProperty(history, 'scrollTop', { configurable: true, writable: true, value: 10 });
    spyOn(history, 'getBoundingClientRect').and.returnValue({ top: 80 } as DOMRect);
    spyOn(verdict, 'getBoundingClientRect').and.returnValue({ top: 330 } as DOMRect);
    spyOn(window, 'matchMedia').and.returnValue({ matches: true } as MediaQueryList);

    fixture.componentInstance.scrollToLatest();

    expect(history.scrollTop).toBe(260);
    fixture.destroy();
  }));

  it('keeps a small scroll-up while an expanded screenshot loads', fakeAsync(() => {
    const fixture = render();
    fixture.componentInstance.chat.set({ ...thread, timeline: [{ ...thread.messages[1], kind: 'step',
      payload: { post_image_name: 'screen.png' } }] });
    fixture.detectChanges();
    const history = fixture.nativeElement.querySelector('.chat-history');
    Object.defineProperty(history, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(history, 'clientHeight', { configurable: true, value: 200 });
    Object.defineProperty(history, 'scrollTop', { configurable: true, writable: true, value: 795 });
    history.dispatchEvent(new Event('scroll'));
    fixture.nativeElement.querySelector('.step-screenshot').dispatchEvent(new Event('load'));
    expect(history.scrollTop).toBe(795);
    fixture.destroy();
  }));
  describe('composer (Sessions v2.1 rev-4)', () => {
    const phone = (over: Partial<DeviceInfo> = {}): DeviceInfo => ({ serial: 'R58M1234a1b2', state: 'device', model: 'Pixel 6 Pro', product: 'raven',
      android_version: '15', screen_resolution: null, is_locked: false, is_emulator: false, device_kind: 'phone', ...over });
    const onPhone = () => api.get.and.returnValue(of({ session_id: 'root', prompt: 'Check login', read_only: false,
      device_ref: { host_id: null, serial: 'R58M1234a1b2' } } as RunSummary));
    const q = (fixture: { nativeElement: HTMLElement }, selector: string) => fixture.nativeElement.querySelector<HTMLElement>(selector)!;
    const running = () => api.chat.and.returnValue(of({ ...thread, turns: [{ ...thread.turns[0], status: 'running' }] }));

    function composer() {
      const fixture = render();
      const tools = q(fixture, '.composer-tools');
      expect(tools).withContext('toolbar under the field').not.toBeNull();
      return { fixture, field: q(fixture, '#session-message') as HTMLTextAreaElement, tools };
    }

    it('is a two-line field with the toolbar below it', fakeAsync(() => {
      const { fixture, field, tools } = composer();
      expect(field.rows).toBe(2);
      expect(field.compareDocumentPosition(tools) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(tools.contains(field)).toBeFalse();
      fixture.destroy();
    }));

    it('keeps the field label for assistive tech but not on screen', fakeAsync(() => {
      const { fixture, field } = composer();
      expect(field.labels?.[0]?.classList).toContain('sr-only');
      fixture.destroy();
    }));

    it('puts Pause, Stop and Send in the toolbar as labelled icon buttons', fakeAsync(() => {
      running();
      const { fixture, tools } = composer();
      const buttons = Array.from(tools.querySelectorAll('button')).filter(button => !button.classList.contains('phone-chip'));
      expect(buttons.map(button => button.getAttribute('aria-label'))).toEqual(['Pause', 'Stop', 'Send']);
      expect(buttons.every(button => button.querySelector('svg') && !button.textContent!.trim())).toBeTrue();
      expect(buttons[2].type).toBe('submit');
      fixture.destroy();
    }));

    it('swaps Pause for Continue while paused and keeps the hint under the toolbar', fakeAsync(() => {
      const { fixture, tools } = composer();
      expect(tools.querySelector('[aria-label="Continue"]')).not.toBeNull();
      expect(tools.querySelector('[aria-label="Pause"]')).toBeNull();
      const hint = q(fixture, '.composer-hint');
      expect(tools.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(hint.textContent).toContain('Paused');
      fixture.destroy();
    }));

    it('shows the phone chip with model, status dot and lock, never the serial', fakeAsync(() => {
      onPhone();
      devices.set([phone()]);
      const { fixture, tools } = composer();
      const chip = tools.querySelector<HTMLElement>('.phone-chip')!;
      expect(chip.textContent!.trim()).toBe('Pixel 6 Pro');
      expect(chip.textContent).not.toContain('a1b2');
      expect(chip.querySelector('.dot')!.classList).toContain('ok');
      expect(chip.querySelector('[data-lock]')).not.toBeNull();
      expect(chip.getAttribute('aria-disabled')).toBe('true');
      expect(chip.getAttribute('aria-label')).toContain('Pixel 6 Pro');
      fixture.destroy();
    }));

    for (const [model, product, expected] of [['Pixel 6 Pro', 'raven', 'Pixel 6 Pro'], [null, 'raven', 'raven'], [null, null, 'Android phone']] as const) {
      it(`labels the chip ${expected} when model is ${model} and product is ${product}`, fakeAsync(() => {
        onPhone();
        devices.set([phone({ model, product })]);
        const { fixture, tools } = composer();
        expect(tools.querySelector('.phone-chip .lbl')!.textContent).toBe(expected);
        fixture.destroy();
      }));
    }

    it('keeps the chip and the actions on one toolbar row on a phone-width screen', fakeAsync(() => {
      const { fixture } = composer();
      const narrow = Array.from(document.styleSheets).flatMap(sheet => Array.from(sheet.cssRules))
        .filter((rule): rule is CSSMediaRule => rule instanceof CSSMediaRule && /max-width:\s*600px/.test(rule.conditionText))
        .flatMap(media => Array.from(media.cssRules) as CSSStyleRule[])
        .filter(rule => /phone-chip|composer-tools/.test(rule.selectorText ?? ''));
      expect(narrow.filter(rule => rule.style.flexBasis === '100%' || rule.style.flexWrap === 'wrap').map(rule => rule.selectorText)).toEqual([]);
      fixture.destroy();
    }));

    it('labels an unlisted phone "Android phone" and marks its dot as an error', fakeAsync(() => {
      onPhone();
      const { fixture, tools } = composer();
      expect(tools.querySelector('.phone-chip .lbl')!.textContent).toBe('Android phone');
      expect(tools.querySelector('.phone-chip .dot')!.classList).toContain('error');
      fixture.destroy();
    }));

    describe('when the session phone disconnects', () => {
      const lost = () => { onPhone(); devices.set([]); };
      const other = { serial: 'EMU5554', text: 'Pixel 3a · …5554 · AVD', usable: true, note: null };

      it('disables the field and Send, keeps the draft, and says why', fakeAsync(() => {
        lost();
        const { fixture, field, tools } = composer();
        fixture.componentInstance.draft = 'Keep me';
        fixture.detectChanges(); tick(0); fixture.detectChanges();
        expect(field.disabled).toBeTrue();
        expect(field.placeholder).toBe('Reconnect the phone first');
        expect(fixture.componentInstance.draft).toBe('Keep me');
        expect(tools.querySelector<HTMLButtonElement>('[aria-label="Send"]')!.disabled).toBeTrue();
        fixture.destroy();
      }));

      it('does not send while the phone is gone', fakeAsync(() => {
        lost();
        const { fixture } = composer();
        fixture.componentInstance.draft = 'Try anyway';
        fixture.componentInstance.send();
        expect(api.sendChat).not.toHaveBeenCalled();
        fixture.destroy();
      }));

      it('keeps Stop reachable while a run is live', fakeAsync(() => {
        lost(); running();
        const { fixture, tools } = composer();
        expect(tools.querySelector<HTMLButtonElement>('[aria-label="Stop"]')!.disabled).toBeFalse();
        fixture.destroy();
      }));

      it('turns the chip into an error-dotted, blinking picker button that says Disconnected', fakeAsync(() => {
        lost();
        const { fixture, tools } = composer();
        const chip = tools.querySelector<HTMLElement>('.phone-chip')!;
        expect(chip.classList).toContain('blink');
        expect(chip.getAttribute('aria-disabled')).toBeNull();
        expect(chip.getAttribute('aria-expanded')).toBe('false');
        expect(chip.getAttribute('aria-label')).toContain('Phone disconnected');
        expect(chip.textContent).toContain('Disconnected');
        expect(chip.querySelector('.dot')!.classList).toContain('error');
        expect(chip.querySelector('[data-lock]')).toBeNull();
        fixture.destroy();
      }));

      it('stops blinking for reduced motion', fakeAsync(() => {
        lost();
        const { fixture } = composer();
        const reduced = Array.from(document.styleSheets).flatMap(sheet => Array.from(sheet.cssRules))
          .filter((rule): rule is CSSMediaRule => rule instanceof CSSMediaRule && /prefers-reduced-motion:\s*reduce/.test(rule.conditionText))
          .flatMap(media => Array.from(media.cssRules) as CSSStyleRule[])
          .filter(rule => /blink/.test(rule.selectorText ?? ''));
        expect(reduced.some(rule => rule.style.animationName === 'none' || rule.style.animation === 'none')).toBeTrue();
        fixture.destroy();
      }));

      it('waits for the first phone list before treating the phone as disconnected', fakeAsync(() => {
        lost(); readiness.set(null);
        const { fixture, field, tools } = composer();
        expect(field.disabled).toBeFalse();
        const chip = tools.querySelector('.phone-chip')!;
        expect(chip.classList).not.toContain('blink');
        expect(chip.querySelector('.dot')!.classList).not.toContain('error');
        expect(chip.getAttribute('aria-label')).not.toContain('Disconnected');
        readiness.set({}); fixture.detectChanges(); tick(0);
        expect(field.disabled).toBeTrue();
        fixture.destroy();
      }));

      it('does not blink, disable or open anything while the phone is connected', fakeAsync(() => {
        onPhone(); devices.set([phone()]);
        const { fixture, field, tools } = composer();
        expect(tools.querySelector('.phone-chip')!.classList).not.toContain('blink');
        expect(field.disabled).toBeFalse();
        tools.querySelector<HTMLElement>('.phone-chip')!.click(); fixture.detectChanges();
        expect(q(fixture, '.phone-pop')).toBeNull();
        fixture.destroy();
      }));

      it('opens a popover offering Reconnect and other phones, and closes on Escape', fakeAsync(() => {
        lost(); pickerOptions.set([{ serial: 'R58M1234a1b2', text: 'Pixel 6 Pro · …a1b2', usable: false, note: 'The phone is offline.' }, other]);
        const { fixture, tools } = composer();
        const chip = tools.querySelector<HTMLElement>('.phone-chip')!;
        chip.click(); fixture.detectChanges();
        const pop = q(fixture, '.phone-pop');
        expect(chip.getAttribute('aria-expanded')).toBe('true');
        expect(pop.textContent).toContain('lost connection');
        expect(pop.querySelector('[data-reconnect]')!.textContent).toContain('Reconnect Android phone');
        const choices = Array.from(pop.querySelectorAll('[data-another]'));
        expect(choices.map(choice => choice.textContent!.trim())).toEqual(['Pixel 3a · …5554 · AVD']);
        expect(pop.textContent).toContain('A new session opens with the same first prompt');
        pop.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); fixture.detectChanges();
        expect(q(fixture, '.phone-pop')).toBeNull();
        fixture.destroy();
      }));

      it('Reconnect asks the browser to reconnect the phone and closes the popover', fakeAsync(() => {
        lost();
        const { fixture, tools } = composer();
        tools.querySelector<HTMLElement>('.phone-chip')!.click(); fixture.detectChanges();
        q(fixture, '[data-reconnect]').click(); tick(0); fixture.detectChanges();
        expect(phoneService.connectFromBrowser).toHaveBeenCalledTimes(1);
        expect(q(fixture, '.phone-pop')).toBeNull();
        fixture.destroy();
      }));

      it('disables Reconnect when this browser cannot connect a phone', fakeAsync(() => {
        lost(); phoneService.canConnectFromBrowser.set(false);
        const { fixture, tools } = composer();
        tools.querySelector<HTMLElement>('.phone-chip')!.click(); fixture.detectChanges();
        expect((q(fixture, '[data-reconnect]') as HTMLButtonElement).disabled).toBeTrue();
        fixture.destroy();
      }));

      it('runs the same first prompt on another phone as a new session', fakeAsync(() => {
        lost(); pickerOptions.set([other]);
        api.get.and.returnValue(of({ session_id: 'root', prompt: 'Check login', chat_prompt: 'First prompt of the session', read_only: false,
          device_ref: { host_id: null, serial: 'R58M1234a1b2' } } as RunSummary));
        const { fixture, tools } = composer();
        const emitted = spyOn(fixture.componentInstance.newRunPrompt, 'emit');
        tools.querySelector<HTMLElement>('.phone-chip')!.click(); fixture.detectChanges();
        q(fixture, '[data-another]').click(); fixture.detectChanges();
        expect(phoneService.choose).toHaveBeenCalledOnceWith('EMU5554');
        expect(emitted).toHaveBeenCalledOnceWith('First prompt of the session');
        expect(q(fixture, '.phone-pop')).toBeNull();
        fixture.destroy();
      }));

      it('does not offer other phones that are unusable', fakeAsync(() => {
        lost(); pickerOptions.set([{ ...other, usable: false, note: 'The phone is offline.' }]);
        const { fixture, tools } = composer();
        tools.querySelector<HTMLElement>('.phone-chip')!.click(); fixture.detectChanges();
        expect(fixture.nativeElement.querySelectorAll('[data-another]').length).toBe(0);
        fixture.destroy();
      }));
    });
  });
  describe('step evidence', () => {
    const evidence = (action: Record<string, unknown>, extra: Record<string, unknown> = {}) => {
      const fixture = render();
      fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage({ payload: { step_id: 'step-1', pre_image_name: 'pre.png', post_image_name: 'post.png', action_taken: action, ...extra } })));
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.step-row summary').click();
      fixture.detectChanges();
      const before = fixture.nativeElement.querySelector('.evidence-before') as HTMLElement;
      expect(before).withContext('Before figure in the expanded step').not.toBeNull();
      return { fixture, before, after: fixture.nativeElement.querySelector('.evidence-after') as HTMLElement };
    };

    it('keeps Before and After in one collapsed item that a click opens side by side', fakeAsync(() => {
      const fixture = render();
      fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage()));
      fixture.detectChanges();
      const details = fixture.nativeElement.querySelector('.step-row details') as HTMLDetailsElement;
      expect(details.open).toBeFalse();
      expect(fixture.nativeElement.querySelectorAll('.step-row details').length).toBe(1);
      fixture.nativeElement.querySelector('.step-row summary').click();
      fixture.detectChanges();
      const before = details.querySelector('.evidence-before') as HTMLElement;
      const after = details.querySelector('.evidence-after') as HTMLElement;
      expect(before.querySelector('figcaption')!.textContent).toContain('Before');
      expect((before.querySelector('img') as HTMLImageElement).src).toContain('/images/pre.png');
      expect(after.querySelector('figcaption')!.textContent).toContain('After');
      expect((after.querySelector('img.step-screenshot') as HTMLImageElement).src).toContain('/images/post.png');
      expect(before.getBoundingClientRect().right).toBeLessThanOrEqual(after.getBoundingClientRect().left + 1);
      fixture.destroy();
    }));

    it('marks a tap with a ring at the tap point and names the element', fakeAsync(() => {
      const { fixture, before, after } = evidence({ action: 'tap', normalized_coordinates: [500, 300], target_text: 'first result' });
      const marker = before.querySelector('.action-marker.tap') as HTMLElement;
      expect(marker.style.left).toBe('50%');
      expect(marker.style.top).toBe('30%');
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Tap · first result');
      expect(before.querySelector('img')!.alt).toContain('Tap · first result');
      expect(after.querySelector('.action-marker, .action-caption')).toBeNull();
      fixture.destroy();
    }));

    it('names the model target when the step has no observed element text', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'click', normalized_coordinates: [100, 900], target_description: 'Skip ad button' });
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Tap · Skip ad button');
      fixture.destroy();
    }));

    it('says only the action when the step carries no element name, never bare coordinates', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'tap', normalized_coordinates: [500, 300] });
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Tap');
      expect(before.querySelector('.action-marker.tap')).not.toBeNull();
      fixture.destroy();
    }));

    it('boxes the typed field and shows the typed text', fakeAsync(() => {
      const { fixture, before } = evidence(
        { action: 'input_text', text: 'gangnam style', target_text: 'search field', normalized_coordinates: [500, 100], target_bounds: [108, 120, 972, 360] },
        { extra_metadata: { width: 1080, height: 2400 } });
      const box = before.querySelector('.action-marker.type') as HTMLElement;
      expect(box.style.left).toBe('10%');
      expect(box.style.top).toBe('5%');
      expect(box.style.width).toBe('80%');
      expect(box.style.height).toBe('10%');
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Type "gangnam style" · search field');
      fixture.destroy();
    }));

    it('boxes a typed field from normalized target_bounds (indexed targets record them on the 0 to 1000 scale)', fakeAsync(() => {
      const { fixture, before } = evidence(
        { action: 'input_text', text: 'gangnam style', target_text: 'search field', normalized_coordinates: [500, 75], target_bounds: [100, 50, 900, 100] },
        { extra_metadata: { width: 1080, height: 2400 } });
      const box = before.querySelector('.action-marker.type.box') as HTMLElement;
      expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(['10%', '5%', '80%', '5%']);
      fixture.destroy();
    }));

    it('reads normalized target_bounds as normalized when the step has no tap point', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'input_text', text: 'a', target_bounds: [100, 50, 900, 100] });
      const box = before.querySelector('.action-marker.type.box') as HTMLElement;
      expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(['10%', '5%', '80%', '5%']);
      fixture.destroy();
    }));

    it('reads target_bounds with a value above 1000 as pixels', fakeAsync(() => {
      const { fixture, before } = evidence(
        { action: 'input_text', text: 'a', target_bounds: [108, 1200, 972, 1440] },
        { extra_metadata: { width: 1080, height: 2400 } });
      const box = before.querySelector('.action-marker.type.box') as HTMLElement;
      expect([box.style.left, box.style.top, box.style.width, box.style.height]).toEqual(['10%', '50%', '80%', '10%']);
      fixture.destroy();
    }));

    it('draws a swipe as an arrow from the start to the end point', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'swipe', normalized_start_coordinates: [500, 750], normalized_end_coordinates: [500, 250] });
      const line = before.querySelector('.action-marker.swipe line') as SVGLineElement;
      expect([line.getAttribute('x1'), line.getAttribute('y1'), line.getAttribute('x2'), line.getAttribute('y2')]).toEqual(['50', '75', '50', '25']);
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Swipe up');
      fixture.destroy();
    }));

    it('marks a hardware key press on the phone edge and names the key', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'press_key', key: 'VOLUME_UP' });
      expect(before.querySelector('.action-marker.key')).not.toBeNull();
      expect(before.querySelector('.action-caption')!.textContent!.trim()).toBe('Key · VOLUME_UP');
      fixture.destroy();
    }));

    it('draws no marker and no caption for an action without a screen position', fakeAsync(() => {
      const { fixture, before } = evidence({ action: 'wait_for_delay', time_in_ms: 500 });
      expect(before.querySelector('.action-marker, .action-caption')).toBeNull();
      fixture.destroy();
    }));

    it('says so when the Before screenshot is missing or fails to load, and keeps After', fakeAsync(() => {
      const fixture = render();
      fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage({ payload: { step_id: 'step-1', post_image_name: 'post.png', action_taken: { action: 'tap', normalized_coordinates: [500, 300] } } })));
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.step-row summary').click();
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.evidence-before').textContent).toContain('Before screenshot unavailable');
      expect(fixture.nativeElement.querySelector('.evidence-before .action-marker')).toBeNull();
      expect(fixture.nativeElement.querySelector('.evidence-after img.step-screenshot')).not.toBeNull();
      fixture.componentInstance.chat.set(withTimeline(thread.messages[0], stepMessage({ payload: { step_id: 'step-2', pre_image_name: 'broken.png', post_image_name: 'post.png', action_taken: { action: 'tap' } } })));
      fixture.detectChanges();
      fixture.nativeElement.querySelector('.evidence-before img').dispatchEvent(new Event('error'));
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.evidence-before').textContent).toContain('Before screenshot unavailable');
      fixture.destroy();
    }));
  });
});
