import { Component, signal } from '@angular/core';
import { HttpErrorResponse, HttpEventType, HttpHeaderResponse, HttpHeaders, HttpResponse } from '@angular/common/http';
import { ComponentFixture, TestBed, fakeAsync, tick } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { AgentService } from '../../services/agent.service';
import { Session, SessionUsage } from '../../core/models/session.model';
import { RunSummary, SessionVideo } from '../../core/models/run.model';
import { StepItemData } from '../../core/models/stream.model';
import { RunsService } from '../../services/runs.service';
import { SELECTED_DEVICE_SERIAL_KEY } from '../../services/system.service';
import { RunViewComponent } from './run-view.component';
import { tinyVideoUrl } from './tiny-video.testing';

@Component({ standalone: true, template: 'stub' })
class StubComponent {}

const ID = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';
const START = Date.UTC(2026, 9, 4, 12) / 1000;
const NOW = Math.floor(Date.now() / 1000);

const run = (over: Partial<RunSummary> = {}): RunSummary => ({
  session_id: ID,
  prompt: 'Log in and open settings',
  status: 'completed',
  interrupt_reason: null,
  start_time: START,
  end_time: START + 300,
  host_id: null,
  device_ref: { host_id: null, serial: 'emulator-5554' },
  requested_by: 'qa@example.test',
  pinned: false,
  recordings: [{ recording_id: 'r1', capture: 'stopped', transfer: 'uploaded' }],
  ...over
});

const step = (n: number, over: Partial<StepItemData> = {}): StepItemData => ({
  step_id: `st${n}`,
  step_number: n,
  session_id: ID,
  timestamp: START + n * 10,
  action_taken: { action: 'tap' },
  pre_image_name: `pre${n}.png`,
  post_image_name: `post${n}.png`,
  ...over
});

const ready = (over: Partial<SessionVideo> = {}): SessionVideo => ({
  session_id: ID,
  status: 'ready',
  has_video: true,
  video_url: VIDEO_URL,
  video_segments: [],
  ...over
});

// Real, decodable media: a 404 for a fake URL would fire the element's error event and swap in the fallback.
const VIDEO_URL = tinyVideoUrl();
const VIDEO_A = tinyVideoUrl();
const VIDEO_B = tinyVideoUrl();

const httpError = (status: number, body: unknown = {}) =>
  throwError(() => new HttpErrorResponse({ status, error: body }));

describe('RunViewComponent', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let admin: jasmine.SpyObj<AdminConfigService>;
  let fixture: ComponentFixture<RunViewComponent>;
  let router: Router;
  let root: HTMLElement;
  let clipboard: jasmine.Spy;
  let agent: Pick<AgentService, 'isPaused' | 'pausedError' | 'isRetrying' | 'retryMessage' | 'sessionLogs'
    | 'viewedModel' | 'agentStatus' | 'currentSessionId' | 'runningSessionId'> & {
    sessions: ReturnType<typeof signal<Session[]>>;
    resumeTask: jasmine.Spy;
    stopTask: jasmine.Spy;
    getSessionUsage: jasmine.Spy;
  };

  const q = <T extends Element>(selector: string) => root.querySelector<T>(selector);
  const qa = <T extends Element>(selector: string) => Array.from(root.querySelectorAll<T>(selector));
  const button = (label: string) =>
    qa<HTMLButtonElement>('button').find((b) => (b.textContent ?? '').replace(/\s+/g, ' ').trim().startsWith(label))!;

  async function settle() {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function open(
    {
      runResult = of(run()) as Observable<RunSummary>,
      steps = of([step(1), step(2), step(3)]) as Observable<StepItemData[]>,
      video = of(ready()) as Observable<SessionVideo>,
      isAdmin = false,
      email = 'qa@example.test',
      id = ID,
      viewMode = 'review' as 'live' | 'review'
    } = {}
  ) {
    runs.get.and.returnValue(runResult);
    runs.steps.and.returnValue(steps);
    runs.video.and.returnValue(video);
    admin.getIdentity.and.returnValue(
      of({ email, admin: isAdmin, auth_mode: 'cloudflare', reason: null })
    );
    fixture = TestBed.createComponent(RunViewComponent);
    fixture.componentRef.setInput('runId', id);
    fixture.componentRef.setInput('mode', viewMode);
    root = fixture.nativeElement;
    root.style.height = '480px';
    await settle();
  }

  beforeEach(async () => {
    localStorage.removeItem(SELECTED_DEVICE_SERIAL_KEY);
    runs = jasmine.createSpyObj<RunsService>(
      'RunsService',
      ['get', 'steps', 'video', 'pin', 'unpin', 'remove', 'downloadBundle'],
      { lastLibraryQuery: signal<Record<string, string>>({ status: 'failed' }), viewPosition: signal(null) }
    );
    Object.assign(runs, {
      checks: jasmine.createSpy('checks').and.returnValue(of({ records: [], streams: [], run_outcome: null })),
      notes: jasmine.createSpy('notes').and.returnValue(of({ notes: {} }))
    });
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    agent = {
      isPaused: signal(false),
      pausedError: signal<string | null>(null),
      isRetrying: signal(false),
      retryMessage: signal<string | null>(null),
      sessionLogs: signal<any[]>([]),
      sessions: signal<Session[]>([]),
      viewedModel: signal({ name: 'Pro', id: 'model-pro', provider: 'test' }),
      agentStatus: signal('idle'),
      currentSessionId: signal<string | null>(ID),
      runningSessionId: signal<string | null>(ID),
      resumeTask: jasmine.createSpy('resumeTask'),
      stopTask: jasmine.createSpy('stopTask'),
      getSessionUsage: jasmine.createSpy('getSessionUsage').and.returnValue(of({
        session_id: ID, llm_calls: 2, prompt_tokens: 100, completion_tokens: 50, total_tokens: 150,
        cached_tokens: 10, operator_context_tokens: 80, operator_context_window_tokens: 1000
      } satisfies SessionUsage))
    };
    clipboard = spyOn(navigator.clipboard, 'writeText').and.resolveTo();
    await TestBed.configureTestingModule({
      imports: [RunViewComponent],
      providers: [
        provideRouter([
          { path: 'runs', component: StubComponent },
          { path: 'runs/:id', component: StubComponent },
          { path: 'workspace', component: StubComponent }
        ]),
        { provide: RunsService, useValue: runs },
        { provide: AdminConfigService, useValue: admin },
        { provide: AgentService, useValue: agent }
      ]
    }).compileComponents();
    router = TestBed.inject(Router);
  });

  describe('restored result summary and execution details', () => {
    it('uses a short title and keeps the complete prompt in a closed, 44 px disclosure', async () => {
      const prompt = '\n## **Open** _Settings_ with `Android`\n- Check every toggle.';
      const previousTitle = document.title;
      await open({ runResult: of(run({ prompt })) });
      expect(q('h1.run-prompt')?.textContent).toBe('Open Settings with Android');
      expect(document.title).toBe('Open Settings with Android · SmartQA');
      const disclosure = q<HTMLDetailsElement>('.full-prompt')!;
      const control = disclosure.querySelector('summary')!;
      expect(disclosure.open).toBeFalse();
      expect(control.textContent).toBe('Show prompt');
      expect(control.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
      expect(control.getBoundingClientRect().width).toBeGreaterThanOrEqual(44);
      control.click();
      expect(disclosure.open).toBeTrue();
      expect(disclosure.querySelector('pre')?.textContent).toBe(prompt);
      fixture.destroy();
      expect(document.title).toBe(previousTitle);
    });

    it('updates the title and closes the full prompt when another run is selected', async () => {
      await open({ runResult: of(run({ prompt: '**First**\nDetails' })) });
      q<HTMLDetailsElement>('.full-prompt')!.open = true;
      runs.get.and.returnValue(of(run({ session_id: 'second', prompt: '# Second\nOther details' })));
      fixture.componentRef.setInput('runId', 'second');
      await settle();
      expect(q('h1.run-prompt')?.textContent).toBe('Second');
      expect(document.title).toBe('Second · SmartQA');
      expect(q<HTMLDetailsElement>('.full-prompt')!.open).toBeFalse();
    });

    for (const viewMode of ['live', 'review'] as const) {
      it(`shows the persisted task report at the top in ${viewMode} mode`, async () => {
        (runs as any).notes.and.returnValue(of({ notes: { 'output.md': '# Task report\nSettings verified.' } }));
        await open({ viewMode });
        expect(q('.run-result-summary')?.textContent).toContain('Settings verified.');
        expect(q('.run-result-summary')!.compareDocumentPosition(q('.evidence-grid')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      });

      it(`expands and collapses step actions, output and stream in ${viewMode} mode`, async () => {
        await open({ viewMode, steps: of([step(1, {
          operator_native_thinking: 'Find the settings button',
          operator_raw_thinking: 'Opening settings',
          action_taken: { action: 'tap', args: { x: 40, y: 80, text: 'Settings' } },
          last_execution_result: { status: 'success', message: 'Tap delivered' },
          generic_tools: [{ name: 'read_note', args: { name: 'plan.md' }, result: 'Settings plan' }]
        }), step(2)]) });
        const toggles = qa<HTMLButtonElement>('.step-toggle');
        expect(toggles.length).toBe(2);
        expect(toggles[0].getAttribute('aria-expanded')).toBe('false');
        expect(q('.step-details')).toBeNull();
        toggles[0].click();
        await settle();
        expect(toggles[0].getAttribute('aria-expanded')).toBe('true');
        expect(q('.step-details')?.id).toBe(toggles[0].getAttribute('aria-controls')!);
        expect(q('.step-details')?.textContent).toContain('Find the settings button');
        expect(q('.step-details')?.textContent).toContain('Opening settings');
        expect(q('.step-details')?.textContent).toContain('Tap delivered');
        expect(q('.step-details')?.textContent).toContain('Settings plan');
        toggles[1].click();
        await settle();
        expect(qa('.step-details').length).toBe(2);
        toggles[0].click();
        await settle();
        expect(toggles[0].getAttribute('aria-expanded')).toBe('false');
        expect(qa('.step-details').length).toBe(1);
      });
    }

    it('loads checker counts, findings, verdicts and persisted reasoning in review mode', async () => {
      (runs as any).checks.and.returnValue(of({
        records: [{ attempt_id: 'check-1', checkpoint_id: 'final', anchor_step_id: 'st1',
          item_text: 'Settings are visible', kind: 'assert', status: 'passed', evidence: 'Title visible', ts: START + 15 }],
        streams: [{ attempt_id: 'check-1', segments: [{ execution_id: 'exec-1', role: 'thought', when: START + 14, text: 'Inspect the title' }] }],
        run_outcome: { task_status: 'completed', tests: { passed: 1, failed: 0, inconclusive: 0, unchecked: 0 }, last_findings: ['No issues found'] }
      }));
      await open();
      expect(q('.run-result-summary')?.textContent).toContain('Passed: 1');
      expect(q('.run-result-summary')?.textContent).toContain('No issues found');
      qa<HTMLButtonElement>('.step-toggle')[0].click();
      await settle();
      expect(q('.step-details')?.textContent).toContain('Settings are visible');
      expect(q('.step-details')?.textContent).toContain('Title visible');
      expect(q('.step-details')?.textContent).toContain('Inspect the title');
    });

    it('uses report-task-status when output.md is absent and escapes report HTML', async () => {
      (runs as any).notes.and.returnValue(of({ notes: {} }));
      await open({ steps: of([step(1, { action_taken: { action: 'report_task_status',
        args: { status: 'completed', explanation: 'Goal met <script>alert(1)</script>' } } })]) });
      expect(q('.run-result-summary')?.textContent).toContain('Goal met');
      expect(q('.run-result-summary script')).toBeNull();
    });

    it('keeps explicit unavailable summary states without hiding steps when checks or notes fail', async () => {
      (runs as any).notes.and.returnValue(httpError(500));
      (runs as any).checks.and.returnValue(httpError(500));
      await open();
      expect(q('.run-result-summary')?.textContent).toContain('Could not load the task report.');
      expect(q('.run-result-summary')?.textContent).toContain('Could not load checker results.');
      expect(qa('.step-button').length).toBe(3);
    });

    it('retains live checker outcomes without showing a finished verdict during a run', async () => {
      await open({ viewMode: 'live', runResult: of(run({ status: 'running', end_time: null })) });
      agent.sessionLogs.set([{ type: 'checker_event', session_id: ID, timestamp: new Date().toISOString(),
        data: { event: 'run_outcome', session_id: ID, task_status: 'partial', tests: { passed: 2, failed: 1, inconclusive: 0, unchecked: 0 } } }]);
      await settle();
      expect(fixture.componentInstance.resultOutcome()?.tests?.passed).toBe(2);
      expect(fixture.componentInstance.resultOutcome()?.tests?.failed).toBe(1);
      expect(q('.run-result-summary')).toBeNull();
      expect(q('.status-strip')).not.toBeNull();
      agent.currentSessionId.set('another-session');
      await settle();
      expect(fixture.componentInstance.resultOutcome()).toBeNull();
    });

    it('cancels stale reports and checks and resets expansion when navigating to another run', async () => {
      const oldNotes = new Subject<{ notes: Record<string, string> }>();
      const oldChecks = new Subject<any>();
      (runs as any).notes.and.returnValue(oldNotes);
      (runs as any).checks.and.returnValue(oldChecks);
      await open();
      qa<HTMLButtonElement>('.step-toggle')[0].click();
      await settle();
      const nextId = '4f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';
      runs.get.and.returnValue(of(run({ session_id: nextId })));
      (runs as any).notes.and.returnValue(of({ notes: { 'output.md': 'New run report' } }));
      (runs as any).checks.and.returnValue(of({ records: [] }));
      fixture.componentRef.setInput('runId', nextId);
      await settle();
      oldNotes.next({ notes: { 'output.md': 'Wrong old report' } });
      oldChecks.next({ records: [], run_outcome: { tests: { passed: 99 } } });
      await settle();
      expect(q('.run-result-summary')?.textContent).toContain('New run report');
      expect(q('.run-result-summary')?.textContent).not.toContain('Wrong old report');
      expect(q('.run-result-summary')?.textContent).not.toContain('Passed: 99');
      expect(qa('.step-details').length).toBe(0);
    });

    it('keeps unanchored checker output discoverable and renders live checker tools', async () => {
      await open({ viewMode: 'live' });
      agent.sessionLogs.set([
        { type: 'checker_event', session_id: ID, timestamp: new Date().toISOString(),
          data: { event: 'attempt_started', attempt_id: 'unanchored', trace_id: 'check-trace', phase: 'final' } },
        { type: 'trace_recorded', session_id: ID, timestamp: new Date().toISOString(),
          data: { type: 'tool', parent_trace_id: 'check-trace', name: 'read_note', result: 'Live checker output' } },
        { type: 'checker_event', session_id: ID, timestamp: new Date().toISOString(),
          data: { event: 'attempt_finished', attempt_id: 'unanchored', phase: 'final', status: 'done',
            verdicts: [{ kind: 'assert', item_text: 'Goal verified', status: 'passed', evidence: 'Matching title' }] } }
      ]);
      await settle();
      expect(q('.checker-results')?.textContent).toContain('Goal verified');
      expect(q('.checker-results')?.textContent).toContain('Live checker output');
    });
  });

  describe('U1 presentation characterization', () => {
    const statuses = [
      ['pending', 'Queued', 'schedule', 'neutral'],
      ['running', 'Running', 'play_circle', 'neutral'],
      ['paused', 'Paused', 'pause_circle', 'neutral'],
      ['completed', 'Passed', 'check_circle', 'ok'],
      ['success', 'Passed', 'check_circle', 'ok'],
      ['failed', 'Failed', 'cancel', 'danger'],
      ['interrupted', 'Interrupted', 'warning', 'warn'],
      ['cancelled', 'Cancelled', 'block', 'neutral'],
      ['future_status', 'Unknown', 'help', 'neutral']
    ];
    for (const [status, label, icon, tone] of statuses) {
      it(`preserves the ${status} badge`, async () => {
        await open({ runResult: of(run({ status })) });
        const badge = q('.outcome-badge')!;
        expect(badge.classList.contains(`tone-${tone}`)).toBeTrue();
        expect(badge.textContent!.replace(/\s+/g, ' ').trim()).toBe(`${icon} ${label}`);
        expect(badge.querySelector('span')!.getAttribute('aria-hidden')).toBe('true');
      });
    }

    const recordings: Array<[string, string | null, string | null, SessionVideo['status'] | null, string, boolean]> = [
      ['in_progress', 'recording', null, 'ready', 'Recording in progress', false],
      ['pending', 'pending', null, 'ready', 'The recording has not started yet.', false],
      ['waiting_for_computer', 'stopped', 'waiting_for_computer', 'ready', 'Video will appear when your computer reconnects.', false],
      ['uploading', 'stopped', 'uploading', 'ready', 'Uploading video…', false],
      ['upload_failed', 'stopped', 'failed', 'ready', 'Video upload failed. Retry upload runs from the computer.', false],
      ['preparing', 'stopped', 'uploaded', 'processing', 'Preparing video…', true],
      ['prepare_failed', 'stopped', 'uploaded', 'failed', 'The video could not be prepared.', true],
      ['uploaded_unchecked', 'stopped', 'uploaded', null, 'Video uploaded. Checking that it can play…', true],
      ['missing', 'missing:device_offline', 'uploaded', 'ready', 'No recording: the phone went offline.', false],
      ['none', null, null, 'unavailable', 'No recording for this run.', false],
      ['unknown', null, null, null, 'Recording status unknown.', false]
    ];
    for (const [state, capture, transfer, status, copy, retry] of recordings) {
      it(`preserves ${state} recording copy and screenshot fallback`, async () => {
        await open({
          runResult: of(run({ recordings: [{ recording_id: 'r1', capture, transfer }] })),
          video: status === null ? new Subject<SessionVideo>() : of(ready({ status }))
        });
        expect(q('video')).toBeNull();
        expect(q('.recording-copy')!.textContent!.trim()).toContain(copy);
        expect(q('.recording-copy')!.getAttribute('role')).toBe('status');
        expect(q('img.evidence-image')!.getAttribute('alt')).toBe('Screenshot for step 3');
        expect(qa('button').some((control) => control.textContent!.trim() === 'Check again')).toBe(retry);
      });
    }

    it('preserves device detail, step markup and action order', async () => {
      await open({ steps: of([step(1)]) });
      expect(q('.secondary-meta dd')!.textContent!.trim()).toBe('emulator-5554');
      expect(q('.step-number')!.textContent!.trim()).toBe('Step 1');
      expect(q('.step-title')).not.toBeNull();
      expect(q('.step-button')!.getAttribute('aria-current')).toBe('step');
      expect(qa('.actions button').map((control) => control.textContent!.trim())).toEqual(['Copy link', 'Download', 'Pin', 'Delete']);
    });

    it('preserves badge and step dimensions while using Workbench action tokens', async () => {
      await open({ steps: of([step(1, { action_taken: { action: 'tap', status: 'failed' } })]) });
      const badge = getComputedStyle(q('.outcome-badge')!);
      expect(badge.display).toBe('inline-flex');
      expect(badge.padding).toBe('4px 12px');
      expect(badge.borderRadius).toBe('999px');
      expect(badge.fontWeight).toBe('600');
      expect(getComputedStyle(q('.step-number')!).fontSize).toBe('12px');
      expect(getComputedStyle(q('.step-failed')!).display).toBe('flex');
      expect(getComputedStyle(q('.step-failed')!).fontSize).toBe('12px');
      for (const control of qa('.actions button')) {
        const style = getComputedStyle(control);
        expect(style.minHeight).toBe('44px');
        expect(style.padding).toBe('0px 18px');
        expect(style.borderRadius).toBe(getComputedStyle(document.documentElement).getPropertyValue('--radius-md').trim());
        expect(style.borderTopWidth).toBe('0px');
        expect(style.boxShadow).toBe('none');
        expect(style.fontSize).toBe('14px');
      }
    });

    it('preserves evidence media sizing and fallback message spacing', async () => {
      await open({ video: new Subject<SessionVideo>() });
      const image = getComputedStyle(q('.evidence-image')!);
      expect(image.display).toBe('block');
      expect(image.objectFit).toBe('contain');
      expect(image.backgroundColor).toBe('rgb(24, 24, 27)');
      const copy = getComputedStyle(q('.recording-copy')!);
      expect(copy.margin).toBe('0px 0px 8px');
      expect(copy.fontWeight).toBe('600');
    });
  });

  for (const viewMode of ['live', 'review'] as const) {
    describe(`${viewMode} mode parity`, () => {
      const failure = 'Invalid target index 2. The list is empty on this screen';

      it('shows the same failed outcome, reason, numbered action and before/after screenshots', async () => {
        await open({ viewMode, runResult: of(run({ status: 'failed' })),
          steps: of([step(1, { last_execution_result: { success: false, error: failure } })]),
          video: of(ready({ status: 'failed', message: 'Encoder exited before writing a playable file.' })) });
        expect(q('.outcome-badge')!.textContent).toContain('Failed');
        expect(q('.failed-banner')!.textContent).toContain(failure);
        expect(q('.step-failure-detail')!.textContent).toBe(failure);
        expect(q('.step-number')!.textContent).toBe('Step 1');
        expect(q('.step-title')!.textContent).toBe('Tapping Element');
        q<HTMLButtonElement>('.step-toggle')!.click();
        await settle();
        expect(q('img[alt="Before step 1"]')!.getAttribute('src')).toBe('/images/pre1.png');
        expect(q('img[alt="After step 1"]')!.getAttribute('src')).toBe('/images/post1.png');
        expect(q('.recording-copy')!.textContent).toContain('Encoder exited before writing a playable file.');
        expect(button('Check again')).toBeDefined();
        expect(q('video')).toBeNull();
        expect(button('Start new run with this prompt')).toBeDefined();
      });

      it('keeps raw recorder output inside collapsed technical details', async () => {
        const raw = '/usr/share/scrcpy/scrcpy-server: 1 file pushed, 0 skipped.\njava.lang.NoSuchMethodException: IClipboard';
        await open({ viewMode, runResult: of(run({ status: 'failed' })),
          video: of(ready({ status: 'failed', message: 'The screen recorder could not start on this phone.', detail: raw })) });
        expect(q('.recording-copy')!.textContent).toBe('The video could not be prepared. The screen recorder could not start on this phone.');
        const details = q<HTMLDetailsElement>('details.technical-details')!;
        expect(details.open).toBeFalse();
        expect(details.querySelector('summary')!.textContent).toBe('Technical details');
        expect(details.querySelector('.technical-body')!.textContent).toBe(raw);
      });

      it('shows the same interrupted banner and device without selecting a new device', async () => {
        await open({ viewMode, runResult: of(run({ status: 'interrupted', interrupt_reason: 'device_offline' })) });
        expect(q('.interrupted-banner')!.textContent).toContain('Run interrupted at step 3');
        expect(q('.interrupted-banner')!.textContent).toContain('The phone went offline.');
        expect(q('.secondary-meta')!.textContent).toContain('emulator-5554');
        expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBeNull();
      });

      it('supports timeline arrow, Home and End keys and focusable actions', async () => {
        await open({ viewMode });
        const controls = qa<HTMLButtonElement>('.step-button');
        controls[0].focus();
        controls[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
        await settle();
        expect(document.activeElement).toBe(controls[1]);
        expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st2');
        controls[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
        await settle();
        expect(document.activeElement).toBe(controls[2]);
        controls[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
        await settle();
        expect(document.activeElement).toBe(controls[0]);
        button('Copy link').focus();
        button('Copy link').click();
        await settle();
        expect(q<HTMLDialogElement>('dialog')!.open).toBeTrue();
      });

      it('reports no screenshots when neither side of a step has one', async () => {
        await open({ viewMode, steps: of([step(1, { pre_image_name: undefined, post_image_name: undefined })]),
          video: of(ready({ status: 'unavailable' })) });
        q<HTMLButtonElement>('.step-toggle')!.click();
        await settle();
        expect(q('.step-screenshots')!.querySelector('img')).toBeNull();
        expect(q('.evidence')!.textContent).toContain('No screenshots for this run.');
      });

      it('drops an old confirmation when a different run is selected', async () => {
        await open({ viewMode, isAdmin: true });
        button('Delete').click();
        await settle();
        expect(q<HTMLDialogElement>('dialog')!.open).toBeTrue();
        const nextId = '4f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';
        runs.get.and.returnValue(of(run({ session_id: nextId })));
        fixture.componentRef.setInput('runId', nextId);
        await settle();
        expect(q<HTMLDialogElement>('dialog')!.open).toBeFalse();
        expect(fixture.componentInstance.dialogKind()).toBeNull();
        expect(runs.remove).not.toHaveBeenCalled();
      });
    });
  }

  describe('Workbench step timeline', () => {
    it('renders compact rows with action icons, thumbnails, kind and mono time', async () => {
      const image = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aT0YAAAAASUVORK5CYII=';
      await open({ steps: of([step(1, { duration: 1.5, post_image_name: image }), step(2)]) });
      const rows = qa<HTMLButtonElement>('.step-button');
      expect(q('.step-screenshots')).toBeNull();
      expect(rows[0].getBoundingClientRect().height).toBe(56);
      expect(rows[0].querySelector('.step-icon')!.textContent).toBe('ads_click');
      expect(rows[0].querySelector('img')!.getAttribute('src')).toBe(image);
      const thumbnail = rows[0].querySelector('.step-thumbnail')!.getBoundingClientRect();
      expect(thumbnail.width).toBe(56);
      expect(thumbnail.height).toBe(40);
      expect(rows[0].querySelector('.step-kind')!.textContent).toBe('tap');
      expect(rows[0].querySelector('.step-duration')!.textContent).toBe('1.5s');
      expect(rows[1].querySelector('.step-duration')!.textContent).toBe('0:20');
      expect(getComputedStyle(rows[0].querySelector('.step-duration')!).fontFamily).toContain('mono');
      expect(getComputedStyle(rows[0].querySelector('.step-duration')!).fontSize).toBe('12px');
    });

    it('labels and tints a failed step without relying on color alone', async () => {
      await open({ steps: of([step(1, { last_execution_result: { success: false, error: 'Target not found' } })]) });
      const row = q<HTMLButtonElement>('.step-button')!;
      expect(row.querySelector('.step-failed')!.textContent).toContain('Failed');
      expect(row.querySelector('.step-icon')!.textContent).toBe('error');
      expect(row.classList.contains('failed')).toBeTrue();
      expect(q('.step-failure-detail')!.textContent).toBe('Target not found');
      expect(row.getBoundingClientRect().height).toBe(56);
    });

    it('toggles inline details with a separate 44px control without changing selection', async () => {
      await open({});
      const toggle = q<HTMLButtonElement>('.step-toggle')!;
      const selected = fixture.componentInstance.selectedStep()?.step_id;
      expect(toggle.getBoundingClientRect().width).toBeGreaterThanOrEqual(44);
      expect(toggle.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      toggle.click();
      await settle();
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(q('#step-details-st1')).not.toBeNull();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe(selected);
      toggle.click();
      await settle();
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(q('#step-details-st1')).toBeNull();
    });

    it('uses the same selection for a verdict jump, timeline and evidence pane', async () => {
      await open({ video: of(ready({ status: 'unavailable' })) });
      expect(fixture.componentInstance.goToStep(1)).toBeTrue();
      await settle();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      expect(q('.step-button[aria-current="step"]')!.textContent).toContain('Step 1');
      expect(q('.evidence-image')!.getAttribute('src')).toBe('/images/post1.png');
      expect(document.activeElement).toBe(q('.step-button'));
      expect(fixture.componentInstance.goToStep(999)).toBeFalse();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
    });

    it('keeps keyboard selection bounded and leaves Tab to the browser', async () => {
      await open({});
      const rows = qa<HTMLButtonElement>('.step-button');
      rows[0].focus();
      rows[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
      await settle();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      rows[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
      await settle();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st3');
      const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
      rows[2].dispatchEvent(tab);
      expect(tab.defaultPrevented).toBeFalse();
    });
  });

  describe('live updates and review continuity', () => {
    it('merges arriving and updated steps with persisted steps without dropping selection', async () => {
      await open({ viewMode: 'live', steps: of([step(1)]), runResult: of(run({ status: 'running' })) });
      fixture.componentRef.setInput('liveSteps', [step(2)]);
      await settle();
      q<HTMLButtonElement>('.step-button')!.click();
      await settle();
      fixture.componentRef.setInput('liveSteps', [step(2, {
        last_execution_result: { success: false, error: 'Target disappeared' }
      }), step(3), step(4, { session_id: 'another-run' })]);
      await settle();
      expect(qa('.step-button').length).toBe(3);
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      expect(q('.step-failure-detail')!.textContent).toBe('Target disappeared');
    });

    it('shows incoming steps when the catalog has not indexed a new live run yet', async () => {
      await open({ viewMode: 'live', runResult: httpError(404), steps: of([]) });
      fixture.componentRef.setInput('liveSession', {
        session_id: ID, initial_goal: 'New live task', start_time: START, status: 'running'
      });
      fixture.componentRef.setInput('liveSteps', [step(1)]);
      await settle();
      expect(q('.run-prompt')!.textContent).toBe('New live task');
      expect(q('.outcome-badge')!.textContent).toContain('Running');
      expect(qa('.step-button').length).toBe(1);
    });

    it('keeps selection and both scroll positions after a live view is destroyed and reopened for review', async () => {
      const manySteps = Array.from({ length: 70 }, (_, index) => step(index + 1));
      await open({ viewMode: 'live', steps: of(manySteps) });
      q<HTMLButtonElement>('.step-button')!.click();
      await settle();
      q<HTMLElement>('.viewer-scroll')!.scrollTop = 180;
      q<HTMLElement>('.step-list')!.scrollTop = 210;
      q<HTMLElement>('.viewer-scroll')!.dispatchEvent(new Event('scroll'));
      q<HTMLElement>('.step-list')!.dispatchEvent(new Event('scroll'));
      fixture.destroy();
      const saved = runs.viewPosition()!;
      expect(saved.scrollTop).toBe(180);
      expect(saved.timelineScrollTop).toBe(210);
      await open({ viewMode: 'review', steps: of(manySteps), id: ID.slice(0, 8) });
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      expect(q<HTMLElement>('.viewer-scroll')!.scrollTop).toBe(180);
      expect(q<HTMLElement>('.step-list')!.scrollTop).toBe(210);
    });

    it('refreshes final catalog status and evidence without resetting selection on completion', async () => {
      await open({ viewMode: 'live', runResult: of(run({ status: 'running' })) });
      fixture.componentRef.setInput('liveSession', {
        session_id: ID, initial_goal: 'Live task', start_time: START, status: 'running'
      });
      await settle();
      q<HTMLButtonElement>('.step-button')!.click();
      await settle();
      runs.get.and.returnValue(of(run({ status: 'interrupted', interrupt_reason: 'host_disconnected' })));
      fixture.componentRef.setInput('liveSession', {
        session_id: ID, initial_goal: 'Live task', start_time: START, status: 'cancelled'
      });
      await settle();
      expect(q('.outcome-badge')!.textContent).toContain('Interrupted');
      expect(q('.interrupted-banner')!.textContent).toContain('lost its connection');
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      expect(runs.get).toHaveBeenCalledTimes(2);
    });

    it('prefills the existing live new-task box instead of navigating to the same route', async () => {
      await open({ viewMode: 'live', runResult: of(run({ status: 'failed' })) });
      const received = jasmine.createSpy('newRunPrompt');
      fixture.componentInstance.newRunPrompt.subscribe(received);
      button('Start new run with this prompt').click();
      expect(received).toHaveBeenCalledWith('Log in and open settings');
    });
  });

  describe('paused-task recovery characterization', () => {
    beforeEach(() => {
      agent.isPaused.set(true);
      agent.agentStatus.set('paused');
    });

    it('offers a native focusable Continue control that resumes the current paused live run', async () => {
      await open({ viewMode: 'live', runResult: of(run({ status: 'paused' })) });
      const control = button('Continue task');
      expect(control).toBeDefined();
      control.focus();
      expect(document.activeElement).toBe(control);
      expect(control.type).toBe('button');
      expect(control.tabIndex).toBe(0);
      control.click();
      expect(agent.resumeTask).toHaveBeenCalledTimes(1);
    });

    for (const scenario of [
      { name: 'a stored run', viewMode: 'review' as const, viewedId: ID, activeId: ID, paused: true, status: 'paused' },
      { name: 'a different live run', viewMode: 'live' as const, viewedId: 'other-run', activeId: ID, paused: true, status: 'paused' },
      { name: 'a stale paused event', viewMode: 'live' as const, viewedId: ID, activeId: ID, paused: true, status: 'running' },
      { name: 'a stale polled pause', viewMode: 'live' as const, viewedId: ID, activeId: ID, paused: false, status: 'paused' },
      { name: 'a missing active task', viewMode: 'live' as const, viewedId: ID, activeId: null, paused: true, status: 'paused' }
    ]) {
      it(`does not resume the active task from ${scenario.name}`, async () => {
        agent.currentSessionId.set(scenario.viewedId);
        agent.runningSessionId.set(scenario.activeId);
        agent.isPaused.set(scenario.paused);
        agent.agentStatus.set(scenario.status);
        await open({ id: scenario.viewedId, viewMode: scenario.viewMode,
          runResult: of(run({ session_id: scenario.viewedId, status: 'paused' })) });
        expect(button('Continue task')).toBeUndefined();
        fixture.componentInstance.onAction({ id: 'resume', event: new Event('click') });
        expect(agent.resumeTask).not.toHaveBeenCalled();
      });
    }

    it('rechecks the current session before acting on an already-rendered Continue control', async () => {
      await open({ viewMode: 'live', runResult: of(run({ status: 'paused' })) });
      const control = button('Continue task');
      expect(control).toBeDefined();
      agent.currentSessionId.set('other-run');
      control.click();
      expect(agent.resumeTask).not.toHaveBeenCalled();
    });
  });

  describe('live state and run-information characterization', () => {
    it('shows the current paused run reason beside its Continue control', async () => {
      agent.isPaused.set(true);
      agent.agentStatus.set('paused');
      agent.pausedError.set('AI call failed: quota exhausted');
      await open({ viewMode: 'live', runResult: of(run({ status: 'paused' })) });
      expect(q('.live-state')!.getAttribute('role')).toBe('status');
      expect(q('.live-state')!.textContent).toContain('Task paused');
      expect(q('.live-state')!.textContent).toContain('AI call failed: quota exhausted');
      expect(button('Continue task')).toBeDefined();
    });

    it('shows the current retry reason and removes the notice when retry ends', async () => {
      agent.agentStatus.set('running');
      agent.isRetrying.set(true);
      agent.retryMessage.set('AI service is temporarily busy (Attempt 2/3); retrying in 1s...');
      await open({ viewMode: 'live', runResult: of(run({ status: 'running' })) });
      expect(q('.live-state')!.textContent).toContain('Retrying');
      expect(q('.live-state')!.textContent).toContain('Attempt 2/3');
      expect(button('Continue task')).toBeUndefined();
      agent.isRetrying.set(false);
      await settle();
      expect(q('.live-state')).toBeNull();
    });

    for (const viewMode of ['live', 'review'] as const) {
      it(`does not leak the active task reason into a different ${viewMode} run`, async () => {
        agent.isPaused.set(true);
        agent.agentStatus.set('paused');
        agent.pausedError.set('Other task error');
        agent.isRetrying.set(true);
        agent.retryMessage.set('Other task retry');
        agent.runningSessionId.set('other-task');
        await open({ viewMode });
        expect(q('.live-state')).toBeNull();
        expect(root.textContent).not.toContain('Other task');
      });
    }

    it('restores model, session tokens and executor context in a native live details panel', async () => {
      await open({ viewMode: 'live' });
      const info = q<HTMLDetailsElement>('.run-info')!;
      expect(info.querySelector('summary')!.textContent).toContain('Run information');
      info.open = true;
      info.dispatchEvent(new Event('toggle'));
      await settle();
      expect(agent.getSessionUsage).toHaveBeenCalledWith(ID);
      expect(info.textContent).toContain('Pro');
      expect(info.textContent).toContain('150');
      expect(info.textContent).toContain('80 / 1000');
    });

    it('cancels old usage on a live switch and rejects mismatched usage responses', async () => {
      const oldUsage = new Subject<SessionUsage>();
      agent.getSessionUsage.and.returnValue(oldUsage);
      await open({ viewMode: 'live' });
      const info = q<HTMLDetailsElement>('.run-info')!;
      info.open = true;
      info.dispatchEvent(new Event('toggle'));
      await settle();
      expect(oldUsage.observed).toBeTrue();
      const nextId = 'another-task';
      agent.getSessionUsage.and.returnValue(of({ session_id: ID, total_tokens: 9999 }));
      runs.get.and.returnValue(of(run({ session_id: nextId })));
      agent.currentSessionId.set(nextId);
      fixture.componentRef.setInput('runId', nextId);
      await settle();
      expect(oldUsage.observed).toBeFalse();
      expect(root.textContent).not.toContain('9999');
    });

    it('shows a usage failure without displaying stale counts', async () => {
      agent.getSessionUsage.and.returnValue(httpError(500));
      await open({ viewMode: 'live' });
      const info = q<HTMLDetailsElement>('.run-info')!;
      info.open = true;
      info.dispatchEvent(new Event('toggle'));
      await settle();
      expect(info.textContent).toContain('Could not load usage');
    });

    it('does not fetch live usage or show the active model in review mode', async () => {
      await open();
      expect(q('.run-info')).toBeNull();
      expect(agent.getSessionUsage).not.toHaveBeenCalled();
    });

    it('refreshes usage every three seconds only while the active run information is open', fakeAsync(() => {
      runs.get.and.returnValue(of(run({ status: 'running' })));
      runs.steps.and.returnValue(of([step(1)]));
      runs.video.and.returnValue(of(ready()));
      admin.getIdentity.and.returnValue(of({ email: null, admin: false, auth_mode: 'open', reason: null }));
      fixture = TestBed.createComponent(RunViewComponent);
      root = fixture.nativeElement;
      fixture.componentRef.setInput('mode', 'live');
      fixture.componentRef.setInput('runId', ID);
      fixture.detectChanges();
      const info = q<HTMLDetailsElement>('.run-info')!;
      info.open = true;
      info.dispatchEvent(new Event('toggle'));
      fixture.detectChanges();
      tick(0);
      expect(agent.getSessionUsage).toHaveBeenCalledTimes(1);
      tick(3000);
      expect(agent.getSessionUsage).toHaveBeenCalledTimes(2);
      info.open = false;
      info.dispatchEvent(new Event('toggle'));
      fixture.detectChanges();
      tick(6000);
      expect(agent.getSessionUsage).toHaveBeenCalledTimes(2);
      fixture.destroy();
    }));
  });

  describe('streamed thinking and text characterization', () => {
    const logs = [
      { type: 'llm_stream', timestamp: START, data: { execution_id: 'exec-1', step_id: 'st1', stream_type: 'thinking', text: 'Inspect the screen', isCompleted: false } },
      { type: 'llm_stream', timestamp: START + 1, data: { execution_id: 'exec-1', step_id: 'st1', stream_type: 'text', text: '<script>work</script>', isCompleted: false } }
    ];

    it('shows live Thought and Work blocks as escaped text with native disclosure controls', async () => {
      agent.sessionLogs.set(logs);
      await open({ viewMode: 'live' });
      const streams = q('.live-streams')!;
      expect(streams.textContent).toContain('Thought');
      expect(streams.textContent).toContain('Inspect the screen');
      expect(streams.textContent).toContain('Work');
      expect(streams.textContent).toContain('<script>work</script>');
      expect(streams.querySelector('script')).toBeNull();
      expect(streams.querySelectorAll('details > summary').length).toBe(2);
    });

    it('updates a streamed block without duplicating the execution', async () => {
      agent.sessionLogs.set(logs);
      await open({ viewMode: 'live' });
      agent.sessionLogs.set([logs[0], { ...logs[1], data: { ...logs[1].data, text: 'Work completed', isCompleted: true } }]);
      await settle();
      expect(q('.live-streams')!.textContent).toContain('Work completed');
      expect(q('.live-streams')!.textContent).not.toContain('<script>work</script>');
      expect(q('.live-streams')!.querySelectorAll('details').length).toBe(2);
    });

    it('shows a discarded stream reset reason instead of the discarded output', async () => {
      agent.sessionLogs.set([{ ...logs[1], data: { ...logs[1].data, text: '', isReset: true, resetMessage: 'Retrying after invalid output' } }]);
      await open({ viewMode: 'live' });
      expect(q('.live-streams')!.textContent).toContain('Retrying after invalid output');
      expect(q('.live-streams')!.textContent).not.toContain('<script>work</script>');
    });

    for (const viewMode of ['live', 'review'] as const) {
      it(`does not show selected-session streams on another ${viewMode} run`, async () => {
        agent.sessionLogs.set(logs);
        agent.currentSessionId.set('other-task');
        await open({ viewMode });
        expect(q('.live-streams')).toBeNull();
      });
    }
  });

  describe('live task-switch position characterization', () => {
    for (const catalogMissing of [false, true]) {
      it(`saves run A under A and resets run B with catalog ${catalogMissing ? 'missing' : 'present'}`, async () => {
        const manySteps = Array.from({ length: 30 }, (_, index) => step(index + 1));
        await open({ viewMode: 'live', runResult: catalogMissing ? httpError(404) : of(run()), steps: of(manySteps) });
        fixture.componentRef.setInput('liveSession', { session_id: ID, initial_goal: 'Run A', start_time: START, status: 'running' });
        fixture.componentRef.setInput('liveSteps', manySteps);
        await settle();
        fixture.componentInstance.selectStep(manySteps[0]);
        await settle();
        q<HTMLElement>('.viewer-scroll')!.scrollTop = 120;
        q<HTMLElement>('.step-list')!.scrollTop = 180;
        const oldScroll = q<HTMLElement>('.viewer-scroll')!.scrollTop;
        const oldTimeline = q<HTMLElement>('.step-list')!.scrollTop;
        expect(oldScroll).toBeGreaterThan(0);
        expect(oldTimeline).toBeGreaterThan(0);
        const nextId = 'run-b';
        const nextSteps = manySteps.map((item) => ({ ...item, session_id: nextId, step_id: `b-${item.step_id}` }));
        runs.get.and.returnValue(catalogMissing ? httpError(404) : of(run({ session_id: nextId })));
        runs.steps.and.returnValue(of(nextSteps));
        fixture.componentRef.setInput('runId', nextId);
        fixture.componentRef.setInput('liveSession', { session_id: nextId, initial_goal: 'Run B', start_time: START, status: 'running' });
        fixture.componentRef.setInput('liveSteps', nextSteps);
        await settle();
        expect(runs.viewPosition()).toEqual({ sessionId: ID, selectedStepId: 'st1', scrollTop: oldScroll, timelineScrollTop: oldTimeline });
        expect(fixture.componentInstance.selectedStep()?.step_id).toBe('b-st30');
        expect(q<HTMLElement>('.viewer-scroll')!.scrollTop).toBe(0);
        expect(q<HTMLElement>('.step-list')!.scrollTop).toBe(0);
      });
    }
  });

  describe('reading order', () => {
    it('keeps a selected step and its screenshot when later steps arrive', async () => {
      const updates = new Subject<StepItemData[]>();
      await open({ steps: updates, video: of(ready({ status: 'unavailable' })) });
      updates.next([step(1), step(2)]);
      await settle();
      q<HTMLButtonElement>('.step-button')!.click();
      await settle();
      updates.next([step(1), step(2), step(3)]);
      await settle();
      expect(fixture.componentInstance.selectedStep()?.step_id).toBe('st1');
      expect(q('.step-button')!.getAttribute('aria-current')).toBe('step');
      expect(q('img.evidence-image')!.getAttribute('src')).toBe('/images/post1.png');
    });

    describe('long agent report', () => {
      const LONG_REPORT = [
        '## Báo cáo kiểm thử',
        '',
        '**Kết quả chính:** Không đăng nhập được vì nút Tiếp tục không phản hồi.',
        '',
        '| Bước | Trạng thái |',
        '| --- | --- |',
        '| 1 | Đạt |',
        '| 2 | Lỗi |',
        '',
        ...Array.from({ length: 80 }, (_, i) => `- Chi tiết số ${i + 1}: thiết bị không phản hồi sau khi chạm.`)
      ].join('\n');
      const reportStep = (explanation: string) => step(1, {
        action_taken: { action: 'report_task_status', args: { status: 'failed', explanation } }
      });

      it('summarises the reason and folds the full markdown report into a disclosure', async () => {
        await open({ runResult: of(run({ status: 'failed' })), steps: of([reportStep(LONG_REPORT)]) });
        const summary = 'Kết quả chính: Không đăng nhập được vì nút Tiếp tục không phản hồi.';
        expect(q('.failed-banner > p:nth-child(2)')!.textContent).toBe(summary);
        const details = q<HTMLDetailsElement>('.failed-banner details.agent-report')!;
        expect(details.querySelector('summary')!.textContent).toBe('Agent report');
        expect(details.open).toBeFalse();
        expect(details.querySelector('.report-body table th')!.textContent).toBe('Bước');
        expect(details.querySelectorAll('.report-body td').length).toBe(4);
        expect(getComputedStyle(details.querySelector('summary')!).minHeight).toBe('44px');
        expect(getComputedStyle(details.querySelector('.report-body')!).overflow).toBe('auto');
        expect(q('.step-failure-detail')!.textContent).toBe(summary);
        expect(q('.step-failure-detail')!.hasAttribute('title')).toBeFalse();
        expect(getComputedStyle(q('.step-failure-detail')!).whiteSpace).toBe('nowrap');
      });

      it('keeps the full text as a tooltip when it is short enough', async () => {
        const mid = `${'a'.repeat(300)} end`;
        await open({ runResult: of(run({ status: 'failed' })), steps: of([reportStep(mid)]) });
        expect(q('.step-failure-detail')!.getAttribute('title')).toBe(mid);
        expect(q('.step-failure-detail')!.textContent!.length).toBeLessThanOrEqual(280);
      });

      it('shows a short reason exactly as before, without a disclosure', async () => {
        await open({ runResult: of(run({ status: 'failed' })), steps: of([reportStep('Login button missing')]) });
        expect(q('.failed-banner > p:nth-child(2)')!.textContent).toBe('Login button missing');
        expect(q('.step-failure-detail')!.textContent).toBe('Login button missing');
        expect(q('details.agent-report')).toBeNull();
      });

      it('never renders markup from the report as elements', async () => {
        await open({ runResult: of(run({ status: 'failed' })),
          steps: of([reportStep(`## Report\n\n<img src=x onerror=alert(1)>\n\n${'x'.repeat(300)}`)]) });
        const body = q('.report-body')!;
        expect(body.querySelector('img')).toBeNull();
        expect(body.textContent).toContain('<img src=x onerror=alert(1)>');
      });
    });

    it('marks an execution error as failed without depending on the run status', async () => {
      await open({ steps: of([step(1, {
        last_execution_result: { success: false, error: 'Invalid target index 2. The list is empty on this screen' }
      })]) });
      expect(q('.step-failed')!.textContent).toContain('Failed');
      expect(q('.outcome-badge')!.textContent).toContain('Passed');
    });

    it('opens the share trust dialog from a focusable action and restores focus', async () => {
      await open();
      q<HTMLDetailsElement>('.more-actions')!.open = true;
      const share = button('Copy link');
      share.focus();
      share.click();
      await settle();
      expect(q<HTMLDialogElement>('dialog')!.open).toBeTrue();
      button('Cancel').click();
      await settle();
      expect(document.activeElement).toBe(q('.more-actions > summary'));
    });

    it('keeps actions in the header, then evidence with steps and technical details', async () => {
      await open();
      const order = qa('[data-section], details.technical-details').map(
        (el) => el.getAttribute('data-section') ?? 'technical'
      );
      expect(order).toEqual(['outcome', 'actions', 'evidence', 'steps', 'technical']);
      const outcome = q('[data-section="outcome"]')!;
      expect(outcome.querySelector('.outcome-badge')!.textContent).toContain('Passed');
      expect(outcome.querySelector('.outcome-badge .material-symbols-outlined')).not.toBeNull();
      expect(outcome.querySelector('h1.run-prompt')!.textContent).toContain('Log in and open settings');
    });

    it('requests the run first, then its steps and playback by the resolved full id', async () => {
      await open({ id: '3f2b9c1a' });
      expect(runs.get).toHaveBeenCalledWith('3f2b9c1a');
      expect(runs.steps).toHaveBeenCalledWith(ID);
      expect(runs.video).toHaveBeenCalledWith(ID);
    });

    it('never touches the selected device or the live session', async () => {
      localStorage.setItem(SELECTED_DEVICE_SERIAL_KEY, 'R58M123');
      await open();
      expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe('R58M123');
    });

    it('links back to the library with the remembered filters', async () => {
      await open();
      const back = q<HTMLAnchorElement>('a.back-to-runs')!;
      expect(back.getAttribute('href')).toBe('/runs?status=failed');
    });
  });

  describe('page states', () => {
    it('shows a skeleton while the run loads', async () => {
      await open({ runResult: new Subject<RunSummary>() });
      expect(q('.skeleton')).not.toBeNull();
      expect(q('[data-section="outcome"]')).toBeNull();
    });

    it('shows "Run not found" with a way back for an unknown id', async () => {
      await open({ runResult: httpError(404, { error: 'not_found' }) });
      expect(q('.state-page h1')!.textContent).toContain('Run not found');
      expect(q('.state-page a.back-to-runs')).not.toBeNull();
    });

    it('shows "This run was removed" and the reason for a removed run, not a blank screen', async () => {
      await open({ runResult: httpError(410, { error: 'removed', reason: 'retention', deleted_at: START }) });
      expect(q('.state-page h1')!.textContent).toContain('This run was removed');
      expect(q('.state-page')!.textContent).toContain('retention');
      expect(q('.state-page a.back-to-runs')).not.toBeNull();
    });

    for (const prefix of ['/', '/preview/pr/70/']) {
      for (const status of [401, 403, 0]) {
        it(`keeps the sign-in link under ${prefix} when the check answers ${status}`, async () => {
          spyOnProperty(document, 'baseURI', 'get').and.returnValue(new URL(prefix, location.href).href);
          await open({ runResult: httpError(status) });
          expect(q('.state-page h1')!.textContent).toContain('Sign in to open this run');
          expect(q<HTMLAnchorElement>('.state-page a.sign-in')!.getAttribute('href')).toBe(`${prefix}runs/${ID}`);
        });
      }
    }

    it('lists candidates when a short id matches several runs', async () => {
      await open({
        id: '3f2b9c1a',
        runResult: httpError(409, { error: 'ambiguous_prefix', candidates: [ID, '3f2b9c1a-0000-4000-8000-000000000000'] })
      });
      expect(q('.state-page h1')!.textContent).toContain('More than one run');
      expect(qa('.state-page a.candidate').map((a) => a.getAttribute('href'))).toEqual([
        `/runs/${ID}`,
        '/runs/3f2b9c1a-0000-4000-8000-000000000000'
      ]);
    });

    it('shows "Couldn\'t load this run" with Retry that asks again', async () => {
      await open({ runResult: httpError(500) });
      expect(q('.state-page h1')!.textContent).toContain("Couldn't load this run");
      runs.get.and.returnValue(of(run()));
      button('Retry').click();
      await settle();
      expect(q('[data-section="outcome"]')).not.toBeNull();
    });
  });

  describe('evidence', () => {
    it('is never blocked on video: steps and screenshot show while playback is still loading', async () => {
      await open({ video: new Subject<SessionVideo>() });
      expect(qa('ol.step-list li').length).toBe(3);
      expect(q('img.evidence-image')).not.toBeNull();
    });

    it('plays a ready recording in an opaque video element without the native download control', async () => {
      await open();
      const video = q<HTMLVideoElement>('video')!;
      expect(video.getAttribute('src')).toBe(VIDEO_URL);
      expect(video.hasAttribute('controls')).toBe(true);
      expect(video.getAttribute('controlslist')).toContain('nodownload');
      expect(q('.recording-ribbon')).toBeNull();
    });

    it('plays a partial recording with the "stopped at mm:ss" ribbon', async () => {
      await open({
        runResult: of(run({ recordings: [{ recording_id: 'r1', capture: 'partial', transfer: 'uploaded' }] })),
        video: of(ready({ video_segments: [{ url: VIDEO_A, duration: 40 }, { url: VIDEO_B, duration: 25 }] }))
      });
      expect(q('video')).not.toBeNull();
      expect(q('.recording-ribbon')!.textContent).toContain('Partial recording (stopped at 01:05)');
    });

    it('falls back to the screenshot and says why there is no video, per the mapping function', async () => {
      await open({
        runResult: of(run({ recordings: [{ recording_id: 'r1', capture: 'stopped', transfer: 'waiting_for_computer' }] })),
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('Video will appear when your computer reconnects.');
      expect(q('img.evidence-image')!.getAttribute('src')).toContain('post3.png');
    });

    it('does not treat uploaded as ready: it says preparing and offers to check again', async () => {
      await open({
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('Preparing video…');
      runs.video.and.returnValue(of(ready()));
      button('Check again').click();
      await settle();
      expect(q('video')).not.toBeNull();
    });

    it('says "No recording for this run" for a legacy run that never had one', async () => {
      await open({
        runResult: of(run({ recordings: [] })),
        video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] })
      });
      expect(q('.recording-copy')!.textContent).toContain('No recording for this run.');
    });

    it('selects a step from the timeline and shows its screenshot', async () => {
      await open({ video: of({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] }) });
      const stepButtons = qa<HTMLButtonElement>('ol.step-list button.step-button');
      expect(stepButtons.length).toBe(3);
      expect(stepButtons[2].getAttribute('aria-current')).toBe('step');
      stepButtons[0].click();
      await settle();
      expect(stepButtons[0].getAttribute('aria-current')).toBe('step');
      expect(stepButtons[2].getAttribute('aria-current')).toBeNull();
      expect(q('img.evidence-image')!.getAttribute('src')).toContain('post1.png');
    });

    it('moves to the segment that holds the selected step\'s time', async () => {
      await open({
        video: of(
          ready({
            video_segments: [
              { url: VIDEO_A, duration: 15, start: 0, offset_ms: 0, duration_ms: 15000 },
              { url: VIDEO_B, duration: 20, start: 15, offset_ms: 20000, duration_ms: 20000 }
            ]
          })
        )
      });
      qa<HTMLButtonElement>('ol.step-list button.step-button')[2].click(); // 30 s into the session
      await settle();
      expect(fixture.componentInstance.activeSegmentIndex()).toBe(1);
      expect(q('video')!.getAttribute('src')).toBe(VIDEO_B);
    });
  });

  describe('interrupted runs', () => {
    const interrupted = () =>
      run({ status: 'interrupted', interrupt_reason: 'host_disconnected', recordings: [{ recording_id: 'r1', capture: 'partial', transfer: 'uploaded' }] });

    it('shows the agreed sentence and the reason in QA language, in the viewer and not as a toast', async () => {
      await open({ runResult: of(interrupted()) });
      const banner = q('.interrupted-banner')!;
      expect(banner.getAttribute('role')).toBe('status');
      expect(banner.textContent).toContain(
        'Run interrupted at step 3. Recording is partial. Reconnecting will not resume this run.'
      );
      expect(banner.textContent).toContain('Your computer lost its connection to SmartQA.');
    });

    it('has a reason for every interrupt reason, and a safe sentence for an unknown one', async () => {
      for (const [reason, text] of [
        ['bridge_closed', 'The browser tab holding the phone closed.'],
        ['device_offline', 'The phone went offline.'],
        ['server_restarted', 'SmartQA restarted.'],
        ['auth_expired', 'The computer\'s session expired.'],
        ['brand_new_reason', 'The run stopped before it finished.']
      ]) {
        await open({ runResult: of(run({ status: 'interrupted', interrupt_reason: reason })) });
        expect(q('.interrupted-banner')!.textContent).toContain(text);
        fixture.destroy();
      }
    });

    it('starts a new run with the same prompt by going to Workspace with the prompt as a draft', async () => {
      await open({ runResult: of(interrupted()) });
      spyOn(router, 'navigate').and.resolveTo(true);
      button('Start new run with this prompt').click();
      expect(router.navigate).toHaveBeenCalledWith(['/workspace'], { state: { draftPrompt: 'Log in and open settings' } });
    });

    it('does not show the banner for a run that finished', async () => {
      await open();
      expect(q('.interrupted-banner')).toBeNull();
    });
  });

  describe('trust notices before share and download', () => {
    const NOTICE = 'Videos and screenshots are not redacted and may contain sensitive information.';
    const CF = "Anyone who passes this site's Cloudflare Access check can open this run.";

    it('asks before copying the link, shows both notices, and copies the full-id link only on confirm', async () => {
      await open();
      button('Copy link').click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      expect(dialog.open).toBe(true);
      expect(dialog.textContent).toContain(NOTICE);
      expect(dialog.textContent).toContain(CF);
      expect(clipboard).not.toHaveBeenCalled();
      dialog.querySelector<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(clipboard).toHaveBeenCalledOnceWith(`${window.location.origin}/runs/${ID}`);
      expect(dialog.open).toBe(false);
      expect(q('.action-feedback')!.textContent).toContain('Link copied');
    });

    it('asks before downloading, shows only the redaction notice, and downloads on confirm', async () => {
      await open();
      button('Download').click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      expect(dialog.textContent).toContain(NOTICE);
      expect(dialog.textContent).not.toContain('Cloudflare Access');
      expect(runs.downloadBundle).not.toHaveBeenCalled();
      const events = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(events as never);
      dialog.querySelector<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.downloadBundle).toHaveBeenCalledWith(ID);
      expect(q('.action-feedback')!.textContent).toContain('Preparing bundle');
    });

    it('shows the size once the server reports it, saves the file, and reports done', async () => {
      await open();
      const events = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(events as never);
      const created = spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      spyOn(URL, 'revokeObjectURL');
      const click = spyOn(HTMLAnchorElement.prototype, 'click').and.stub();
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      events.next(new HttpHeaderResponse({ status: 200, headers: new HttpHeaders({ 'Content-Length': String(84 * 1024 * 1024) }) }));
      await settle();
      expect(q('.action-feedback')!.textContent).toContain('84 MB');
      events.next(new HttpResponse({ status: 200, body: new Blob(['zip']) }));
      events.complete();
      await settle();
      expect(created).toHaveBeenCalled();
      expect(click).toHaveBeenCalled();
      expect(q('.action-feedback')!.textContent).toContain('Download started');
      expect(HttpEventType.Response).toBeDefined();
    });

    it('offers Retry when a download fails, and Retry downloads again without asking twice', async () => {
      await open();
      runs.downloadBundle.and.returnValue(httpError(500) as never);
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(q('.action-error')!.textContent).toContain('Download failed');
      runs.downloadBundle.and.returnValue(of(new HttpResponse({ status: 200, body: new Blob(['zip']) })) as never);
      spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      spyOn(URL, 'revokeObjectURL');
      spyOn(HTMLAnchorElement.prototype, 'click').and.stub();
      button('Retry').click();
      await settle();
      expect(runs.downloadBundle).toHaveBeenCalledTimes(2);
      expect(q('.action-error')).toBeNull();
    });

    it('closes on Escape or Cancel without acting and returns focus to More actions', async () => {
      await open();
      document.body.appendChild(root);
      q<HTMLDetailsElement>('.more-actions')!.open = true;
      const copy = button('Copy link');
      copy.focus();
      copy.click();
      await settle();
      const dialog = q<HTMLDialogElement>('dialog.trust-dialog')!;
      dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
      await settle();
      expect(dialog.open).toBe(false);
      expect(clipboard).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(q('.more-actions > summary'));

      q<HTMLDetailsElement>('.more-actions')!.open = true;
      const download = button('Download');
      download.focus();
      download.click();
      await settle();
      q<HTMLButtonElement>('.dialog-cancel')!.click();
      await settle();
      expect(runs.downloadBundle).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(q('.more-actions > summary'));
      root.remove();
    });

    it('focuses the dialog\'s first control when it opens', async () => {
      await open();
      document.body.appendChild(root);
      button('Copy link').click();
      await settle();
      expect(q('dialog.trust-dialog')!.contains(document.activeElement)).toBe(true);
      root.remove();
    });
  });

  describe('a run that is not mine (CHE-1152)', () => {
    const labels = () => qa('button').map((b) => (b.textContent ?? '').replace(/\s+/g, ' ').trim());

    it('opens read-only for a QA who does not own it: evidence and Copy link stay, every change goes', async () => {
      await open({ email: 'someone.else@example.test' });
      expect(q('[data-section="evidence"]')).not.toBeNull();
      expect(q('[data-section="steps"]')).not.toBeNull();
      expect(labels()).toContain('Copy link');
      expect(labels()).toContain('Download');
      for (const control of ['Delete', 'Pin', 'Unpin', 'Stop', 'Resume', 'Stop task', 'Resume task']) {
        expect(labels()).not.toContain(control);
      }
      expect(q('.read-only-note')!.textContent).toContain('qa@example.test');
    });

    it('treats a run with no owner as read-only for a QA', async () => {
      await open({ email: 'someone.else@example.test', runResult: of(run({ requested_by: null })) });
      expect(labels()).not.toContain('Delete');
      expect(q('.read-only-note')).not.toBeNull();
    });

    it('shows no read-only note and the full actions on my own run', async () => {
      await open();
      expect(q('.read-only-note')).toBeNull();
      expect(labels()).toContain('Pin');
      expect(labels()).toContain('Delete');
    });

    it('lets an admin act on anyone\'s run, including an unowned one', async () => {
      await open({ isAdmin: true, email: 'admin@example.test', runResult: of(run({ requested_by: null })) });
      expect(q('.read-only-note')).toBeNull();
      expect(labels()).toContain('Delete');
      expect(labels()).toContain('Pin');
    });

    it('does not offer changes until it knows who is looking', async () => {
      admin.getIdentity.and.returnValue(new Subject());
      runs.get.and.returnValue(of(run()));
      runs.steps.and.returnValue(of([]));
      runs.video.and.returnValue(of(ready()));
      fixture = TestBed.createComponent(RunViewComponent);
      fixture.componentRef.setInput('runId', ID);
      root = fixture.nativeElement;
      await settle();
      expect(labels()).not.toContain('Delete');
      expect(labels()).not.toContain('Pin');
    });

    it('still lets a viewer start a new run from the same prompt', async () => {
      await open({ email: 'someone.else@example.test', runResult: of(run({ status: 'interrupted' })) });
      expect(labels()).toContain('Start new run with this prompt');
    });
  });

  describe('pin and delete', () => {
    it('keeps a server-marked shared run read-only, including pin and delete handlers', async () => {
      await open({ isAdmin: true, runResult: of(run({ read_only: true })) });
      expect(fixture.componentInstance.actions().map((action) => action.id)).toEqual(['share', 'download']);
      fixture.componentInstance.togglePin(new Event('click'));
      fixture.componentInstance.ask('delete');
      expect(fixture.componentInstance.dialogKind()).toBeNull();
      expect(runs.pin).not.toHaveBeenCalled();
      expect(runs.unpin).not.toHaveBeenCalled();
      expect(runs.remove).not.toHaveBeenCalled();
    });

    it('keeps the team-tab review route read-only even for an administrator', async () => {
      await open({ isAdmin: true });
      fixture.componentRef.setInput('readOnly', true);
      await settle();
      expect(fixture.componentInstance.actions().map((action) => action.id)).toEqual(['share', 'download']);
      fixture.componentInstance.ask('unpin_expired');
      expect(fixture.componentInstance.dialogKind()).toBeNull();
    });

    it('does not resume a paused run when the shared controller is read-only', async () => {
      agent.isPaused.set(true);
      agent.agentStatus.set('paused');
      await open({ isAdmin: true, viewMode: 'live', runResult: of(run({ status: 'paused', read_only: true })) });
      expect(fixture.componentInstance.canResume()).toBeFalse();
      expect(fixture.componentInstance.actions().map((action) => action.id)).toEqual(['share', 'download']);
      fixture.componentInstance.onAction({ id: 'resume', event: new MouseEvent('click') });
      expect(agent.resumeTask).not.toHaveBeenCalled();
    });

    it('pins and unpins with a pressed state', async () => {
      await open();
      const pin = button('Pin');
      expect(pin.getAttribute('aria-pressed')).toBe('false');
      runs.pin.and.returnValue(of({}));
      pin.click();
      await settle();
      expect(runs.pin).toHaveBeenCalledWith(ID);
      expect(button('Unpin').getAttribute('aria-pressed')).toBe('true');
      runs.unpin.and.returnValue(of({}));
      button('Unpin').click();
      await settle();
      expect(runs.unpin).toHaveBeenCalledWith(ID);
      expect(button('Pin')).toBeTruthy();
    });

    it('says so when pinning fails and leaves the state alone', async () => {
      await open();
      runs.pin.and.returnValue(httpError(500));
      button('Pin').click();
      await settle();
      expect(q('.action-error')!.textContent).toContain("Couldn't update the pin");
      expect(button('Pin').getAttribute('aria-pressed')).toBe('false');
    });

    it('confirms before unpinning a run already past its retention date', async () => {
      await open({ runResult: of(run({ pinned: true, expires_at: NOW - 86400 })) });
      runs.unpin.and.returnValue(of({}));
      button('Unpin').click();
      await settle();
      expect(q('dialog.trust-dialog')!.textContent).toContain('past its retention date');
      expect(runs.unpin).not.toHaveBeenCalled();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.unpin).toHaveBeenCalledWith(ID);
    });

    it('shows Delete to the run\'s owner and to admins', async () => {
      await open({ isAdmin: false });
      expect(button('Delete')).toBeTruthy();
      fixture.destroy();
      await open({ isAdmin: true, email: 'admin@example.test' });
      expect(button('Delete')).toBeTruthy();
    });

    it('confirms before deleting and returns to the library afterwards', async () => {
      await open({ isAdmin: true });
      runs.remove.and.returnValue(of({}));
      spyOn(router, 'navigate').and.resolveTo(true);
      button('Delete').click();
      await settle();
      expect(q('dialog.trust-dialog')!.textContent).toContain('This cannot be undone');
      expect(runs.remove).not.toHaveBeenCalled();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      expect(runs.remove).toHaveBeenCalledWith(ID);
      expect(router.navigate).toHaveBeenCalledWith(['/runs'], { queryParams: { status: 'failed' } });
    });
  });

  describe('technical details', () => {
    it('keeps raw logs behind a collapsed disclosure and renders them only once opened', async () => {
      await open();
      const details = q<HTMLDetailsElement>('details.technical-details')!;
      expect(details.open).toBe(false);
      expect(details.querySelector('summary')!.textContent).toContain('Technical details');
      expect(details.querySelector('pre.raw-logs')).toBeNull();
      details.open = true;
      details.dispatchEvent(new Event('toggle'));
      await settle();
      expect(details.querySelector('pre.raw-logs')!.textContent).toContain('"step_id": "st1"');
    });
  });

  describe('Workbench header', () => {
    for (const status of ['completed', 'failed', 'interrupted', 'cancelled']) {
      it(`offers Run again, not Stop run, for ${status}`, async () => {
        await open({ runResult: of(run({ status })) });
        expect(q('[aria-label="Run again"]')).not.toBeNull();
        expect(q('[aria-label="Stop run"]')).toBeNull();
        expect(q('.verdict-slot')).not.toBeNull();
        expect(q('.status-strip')).toBeNull();
      });
    }

    for (const status of ['pending', 'running', 'paused']) {
      it(`offers a scoped Stop run and a status strip for ${status}`, async () => {
        await open({ runResult: of(run({ status })) });
        expect(q('[aria-label="Run again"]')).toBeNull();
        q<HTMLButtonElement>('[aria-label="Stop run"]')!.click();
        expect(agent.stopTask).toHaveBeenCalledOnceWith(ID);
        expect(q('.status-strip')).not.toBeNull();
        expect(q('.verdict-slot')).toBeNull();
      });
    }

    it('never stops another owner\'s run or a read-only run', async () => {
      await open({ runResult: of(run({ status: 'running', requested_by: 'other@example.test' })) });
      expect(q('[aria-label="Stop run"]')).toBeNull();
      fixture.componentInstance.onAction({ id: 'stop', event: new Event('click') });
      expect(agent.stopTask).not.toHaveBeenCalled();
      fixture.componentRef.setInput('readOnly', true);
      await settle();
      expect(q('[aria-label="Run again"]')).toBeNull();
    });

    it('copies the full ID from the crumb and discloses the More actions', async () => {
      await open();
      q<HTMLButtonElement>('.run-header .run-id-copy-button')!.click();
      await settle();
      expect(clipboard).toHaveBeenCalledWith(ID);
      const more = q<HTMLDetailsElement>('.more-actions')!;
      expect(more.open).toBeFalse();
      more.querySelector('summary')!.click();
      await settle();
      expect(more.open).toBeTrue();
      expect(qa('.more-actions button').map(control => control.textContent!.trim()))
        .toEqual(['Copy link', 'Download', 'Pin', 'Delete']);
      more.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await settle();
      expect(more.open).toBeFalse();
      expect(document.activeElement).toBe(more.querySelector('summary'));
      more.querySelector('summary')!.click();
      button('Copy link').click();
      await settle();
      expect(more.open).toBeFalse();
      expect(fixture.componentInstance.dialogKind()).toBe('share');
    });

    it('shows facts without borrowing another run\'s model', async () => {
      await open({ runResult: of(run({ start_time: START, end_time: START + 300 })) });
      expect(q('.run-facts')!.textContent).toContain('App');
      expect(q('.run-facts')!.textContent).toContain('Duration');
      expect(q('.run-facts')!.textContent).toContain('5m 0s');
      expect(q('[data-fact="model"]')!.textContent).toBe('Not recorded');
      agent.sessions.set([{ session_id: 'other-run', initial_goal: 'Other', start_time: START, model_info: { name: 'Other model', id: 'other', provider: 'test' } }]);
      await settle();
      expect(q('[data-fact="model"]')!.textContent).toBe('Not recorded');
      agent.sessions.set([{ session_id: ID, initial_goal: 'This run', start_time: START, model_info: { name: 'Flash', id: 'flash', provider: 'test' } }]);
      await settle();
      expect(q('[data-fact="model"]')!.textContent).toBe('Flash');
    });

    it('ticks Elapsed while active and tears down the clock on destroy', async () => {
      await open({ runResult: of(run({ status: 'running', start_time: Date.now() / 1000 - 60, end_time: null })) });
      const before = fixture.componentInstance.duration();
      await new Promise(resolve => setTimeout(resolve, 1100));
      await settle();
      expect(fixture.componentInstance.duration()).not.toBe(before);
      const stopped = fixture.componentInstance.duration();
      fixture.destroy();
      await new Promise(resolve => setTimeout(resolve, 1100));
      expect(fixture.componentInstance.duration()).toBe(stopped);
    });

    it('gives every header control its own 44 by 44 px box', async () => {
      await open();
      q<HTMLDetailsElement>('.more-actions')!.open = true;
      await settle();
      for (const control of qa<HTMLElement>('.run-header button, .run-header summary, .run-header a')) {
        const bounds = control.getBoundingClientRect();
        expect(bounds.width).withContext(control.textContent ?? '').toBeGreaterThanOrEqual(44);
        expect(bounds.height).withContext(control.textContent ?? '').toBeGreaterThanOrEqual(44);
      }
    });
  });

  describe('review corrections', () => {
    const OTHER = '9a8b7c6d-1111-4222-8333-444455556666';
    const noVideo = (): SessionVideo => ({ session_id: ID, status: 'unavailable', has_video: false, video_url: null, video_segments: [] });
    const whenLoaded = (video: HTMLVideoElement) =>
      new Promise<void>((resolve) =>
        video.readyState > 0 ? resolve() : video.addEventListener('loadedmetadata', () => resolve(), { once: true })
      );

    async function openWith(over: { runFor?: (id: string) => RunSummary; video?: SessionVideo; steps?: StepItemData[] } = {}) {
      runs.get.and.callFake((id: string) => of((over.runFor ?? ((rid: string) => run({ session_id: rid, prompt: `Prompt ${rid.slice(0, 4)}` })))(id)));
      runs.steps.and.returnValue(of(over.steps ?? [step(1), step(2), step(3)]));
      runs.video.and.returnValue(of(over.video ?? noVideo()));
      admin.getIdentity.and.returnValue(of({ email: 'a@x.test', admin: true, auth_mode: 'cloudflare', reason: null }));
      fixture = TestBed.createComponent(RunViewComponent);
      fixture.componentRef.setInput('runId', ID);
      root = fixture.nativeElement;
      await settle();
    }

    async function showOther() {
      fixture.componentRef.setInput('runId', OTHER);
      await settle();
    }

    it('P1: a late Pin success for run A cannot overwrite the viewer showing run B', async () => {
      await openWith();
      const pending = new Subject<unknown>();
      runs.pin.and.returnValue(pending);
      button('Pin').click();
      await showOther();
      expect(q('h1.run-prompt')!.textContent).toContain(`Prompt ${OTHER.slice(0, 4)}`);
      pending.next({});
      pending.complete();
      await settle();
      expect(fixture.componentInstance.run()!.session_id).toBe(OTHER);
      expect(q('h1.run-prompt')!.textContent).toContain(`Prompt ${OTHER.slice(0, 4)}`);
      expect(button('Pin').getAttribute('aria-pressed')).toBe('false');
    });

    it('P1: a late Unpin success and a late Pin failure also leave run B alone', async () => {
      await openWith({ runFor: (id) => run({ session_id: id, pinned: true, prompt: `Prompt ${id.slice(0, 4)}` }) });
      const unpin = new Subject<unknown>();
      runs.unpin.and.returnValue(unpin);
      button('Unpin').click();
      await showOther();
      unpin.next({});
      await settle();
      expect(button('Unpin').getAttribute('aria-pressed')).toBe('true');

      const failing = new Subject<unknown>();
      runs.unpin.and.returnValue(failing);
      button('Unpin').click();
      await settle();
      fixture.componentRef.setInput('runId', ID);
      await settle();
      failing.error(new HttpErrorResponse({ status: 500 }));
      await settle();
      expect(q('.action-error')).toBeNull();
    });

    it('P1: a late bundle download or delete from run A does nothing once B is showing', async () => {
      await openWith();
      const bundle = new Subject<unknown>();
      runs.downloadBundle.and.returnValue(bundle as never);
      const created = spyOn(URL, 'createObjectURL').and.returnValue('blob:x');
      button('Download').click();
      await settle();
      q<HTMLButtonElement>('.dialog-confirm')!.click();
      await settle();
      await showOther();
      bundle.next(new HttpResponse({ status: 200, body: new Blob(['zip']) }));
      await settle();
      expect(created).not.toHaveBeenCalled();
      expect(q('.action-feedback')).toBeNull();
    });

    it('P2: selecting a step seeks a single-file recording (no segments) to that step\'s time', async () => {
      await openWith({ video: ready({ video_segments: [] }), steps: [step(1, { timestamp: START + 2 }), step(2, { timestamp: START + 3 })] });
      const video = q<HTMLVideoElement>('video')!;
      await whenLoaded(video);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[0].click();
      await settle();
      expect(video.currentTime).toBeCloseTo(2, 1);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[1].click();
      await settle();
      expect(video.currentTime).toBeCloseTo(3, 1);
    });

    it('P2: a step before the recording began, or past its end, clamps instead of failing', async () => {
      await openWith({ video: ready(), steps: [step(1, { timestamp: START - 5 }), step(2, { timestamp: START + 500 })] });
      const video = q<HTMLVideoElement>('video')!;
      await whenLoaded(video);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[0].click();
      await settle();
      expect(video.currentTime).toBe(0);
      qa<HTMLButtonElement>('ol.step-list button.step-button')[1].click();
      await settle();
      expect(video.currentTime).toBeLessThanOrEqual(video.duration);
    });

    it('P2: an id the server rejects as invalid (400) shows Run not found, not a retryable error', async () => {
      await open({ id: 'bad id', runResult: httpError(400, { error: 'invalid_session_id' }) });
      expect(q('.state-page h1')!.textContent).toContain('Run not found');
      expect(qa('button').some((b) => b.textContent!.trim() === 'Retry')).toBe(false);
    });

    it('P2: a video file that fails to load falls back to the screenshot with recovery copy', async () => {
      await openWith({ video: ready() });
      expect(q('video')).not.toBeNull();
      q<HTMLVideoElement>('video')!.dispatchEvent(new Event('error'));
      await settle();
      expect(q('video')).toBeNull();
      expect(q('.recording-copy')!.textContent).toContain('could not be played');
      expect(q('img.evidence-image')).not.toBeNull();
      runs.video.and.returnValue(of(ready()));
      button('Check again').click();
      await settle();
      expect(q('video')).not.toBeNull();
    });
  });

  describe('keyboard walkthrough', () => {
    const FOCUSABLE = 'a[href], button, input, select, summary, video, [tabindex]:not([tabindex="-1"])';
    const nameOf = (el: Element) =>
      el.getAttribute('aria-label') || (el.textContent ?? '').replace(/\s+/g, ' ').trim();
    const controls = () =>
      qa<HTMLElement>(FOCUSABLE).filter((el) => !(el as HTMLButtonElement).disabled && el.checkVisibility());

    it('tabs from back link through steps and actions to technical details, all natively focusable', async () => {
      await open({ isAdmin: true });
      q<HTMLDetailsElement>('.more-actions')!.open = true;
      const names = controls().map(nameOf);
      expect(names[0]).toBe('Back to runs');
      const stepsAt = names.findIndex((n) => n.startsWith('Step 1'));
      const copyAt = names.indexOf('Copy link');
      expect(stepsAt).toBeGreaterThan(0);
      expect(copyAt).toBeLessThan(stepsAt);
      expect(names.slice(copyAt, copyAt + 4)).toEqual(['Copy link', 'Download', 'Pin', 'Delete']);
      expect(names[names.length - 1]).toBe('Technical details');
      for (const el of controls()) {
        expect(el.tabIndex).toBeGreaterThanOrEqual(0);
      }
    });

    it('keeps targets at least 24 px, primary actions at least 44 px', async () => {
      await open({ isAdmin: true });
      q<HTMLDetailsElement>('.more-actions')!.open = true;
      for (const el of controls()) {
        if (el.tagName === 'VIDEO') continue;
        const box = el.getBoundingClientRect();
        expect(box.width).toBeGreaterThanOrEqual(24, nameOf(el));
        expect(box.height).toBeGreaterThanOrEqual(24, nameOf(el));
      }
      for (const label of ['Copy link', 'Download', 'Pin', 'Delete']) {
        expect(button(label).getBoundingClientRect().height).toBeGreaterThanOrEqual(44, label);
      }
    });
  });
});
