import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { provideLocationMocks } from '@angular/common/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { NEVER, of } from 'rxjs';
import { routes } from '../../app.routes';
import { RunViewComponent } from '../../components/run-view/run-view.component';
import { By } from '@angular/platform-browser';
import { RunSummary } from '../../core/models/run.model';
import { RunTarget } from '../../core/models/run-target.model';
import { Session } from '../../core/models/session.model';
import { AdminConfigService } from '../../services/admin-config.service';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { RunsService } from '../../services/runs.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { WorkspaceComponent } from './workspace.component';

const ID = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';

describe('Workspace review mode', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let harness: RouterTestingHarness;
  let root: HTMLElement;
  let liveSession: ReturnType<typeof signal<Session | null>>;
  let target: ReturnType<typeof signal<RunTarget | null>>;
  let reconnect: jasmine.Spy;
  const q = (selector: string) => root.querySelector(selector);

  beforeEach(async () => {
    liveSession = signal<Session | null>(null);
    target = signal<RunTarget | null>(null);
    reconnect = jasmine.createSpy('connectFromBrowser').and.resolveTo(undefined);
    runs = jasmine.createSpyObj<RunsService>('RunsService', ['list', 'get', 'steps', 'video', 'checks', 'notes'], {
      lastLibraryQuery: signal<Record<string, string>>({}), viewPosition: signal(null)
    });
    runs.list.and.returnValue(NEVER);
    runs.get.and.returnValue(NEVER);
    runs.steps.and.returnValue(NEVER);
    runs.video.and.returnValue(NEVER);
    runs.checks.and.returnValue(of({ records: [], streams: [], run_outcome: null }));
    runs.notes.and.returnValue(of({ notes: {} }));
    const hosts = jasmine.createSpyObj<HostsService>('HostsService', ['list']);
    hosts.list.and.returnValue(NEVER);
    const admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    admin.getIdentity.and.returnValue(of({ email: null, admin: false, auth_mode: 'open', reason: null }));
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => undefined,
      isCurrentSessionRunning: () => false,
      currentSession: liveSession,
      currentSessionId: () => liveSession()?.session_id ?? null,
      currentStartupProgress: () => [],
      runningSessionId: () => null,
      isPaused: () => false,
      viewedModel: () => null,
      sessionLogs: () => [],
      runTask: jasmine.createSpy('runTask'),
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask'),
      selectSession: jasmine.createSpy('selectSession')
    };
    TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter(routes),
        provideLocationMocks(),
        { provide: AgentService, useValue: agentService },
        { provide: RunsService, useValue: runs },
        { provide: WorkspacePhoneService, useValue: {
          target,
          runInterrupted: () => liveSession()?.status === 'interrupted',
          canConnectFromBrowser: () => true,
          connectFromBrowser: reconnect,
          requestPicker: jasmine.createSpy('requestPicker')
        } },
        { provide: HostsService, useValue: hosts },
        { provide: AdminConfigService, useValue: admin }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    });
    harness = await RouterTestingHarness.create();
    root = harness.fixture.nativeElement;
  });

  async function go(url: string, state?: Record<string, unknown>) {
    await TestBed.inject(Router).navigateByUrl(url, { state });
    harness.fixture.detectChanges();
    await harness.fixture.whenStable();
    harness.fixture.detectChanges();
  }

  function atWidth(width: number, check: (frame: HTMLIFrameElement) => void): void {
    const frame = document.createElement('iframe');
    frame.style.width = `${width}px`;
    frame.style.height = '800px';
    document.body.appendChild(frame);
    const workspace = q('app-workspace')!;
    const parent = workspace.parentNode!;
    const next = workspace.nextSibling;
    try {
      const frameDocument = frame.contentDocument!;
      frameDocument.body.style.cssText = 'margin: 0; height: 100vh';
      for (const style of Array.from(document.head.querySelectorAll('style'))) {
        frameDocument.head.appendChild(style.cloneNode(true));
      }
      frameDocument.body.appendChild(workspace);
      check(frame);
    } finally {
      parent.insertBefore(workspace, next);
      frame.remove();
    }
  }

  for (const width of [1440, 1200, 1024, 1023, 800, 390]) {
    it(`shows list and detail on desktop, only the route's pane below 1024 px at ${width}px`, async () => {
      for (const route of ['/runs', `/runs/${ID}`]) {
        await go(route);
        atWidth(width, frame => {
          const frameDocument = frame.contentDocument!;
          const list = frameDocument.querySelector<HTMLElement>('.run-list-pane')!;
          const detail = frameDocument.querySelector<HTMLElement>('.detail-pane')!;
          const showsList = width >= 1024 || route === '/runs';
          const showsDetail = width >= 1024 || route !== '/runs';
          expect(frame.contentWindow!.getComputedStyle(list).display === 'none').toBe(!showsList);
          expect(frame.contentWindow!.getComputedStyle(detail).display === 'none').toBe(!showsDetail);
          if (width >= 1024) {
            expect(list.getBoundingClientRect().width).toBe(360);
            expect(list.getBoundingClientRect().right).toBeCloseTo(detail.getBoundingClientRect().left, 0);
          }
          const visible = showsList ? list : detail;
          expect(visible.getBoundingClientRect().height).toBeGreaterThan(0);
          expect(visible.getBoundingClientRect().right).toBeLessThanOrEqual(width);
        });
      }
    });
  }

  it('shows the run list beside the shared RunView and new-task box on /workspace', async () => {
    await go('/workspace');
    expect(q('app-run-view')).not.toBeNull();
    expect(q('textarea.dock-textarea')).not.toBeNull();
    expect(q('.workspace-container.review-mode')).toBeNull();
    expect(q('.resizer, .right-panel, app-chat-interface')).toBeNull();
    expect(q('.run-list-pane app-run-library')).not.toBeNull();
    expect(q('.detail-pane app-run-view')).not.toBeNull();
  });

  it('shows a single run list and the empty detail state without the live new-task box on /runs', async () => {
    await go('/runs');
    expect(q('.workspace-container.review-mode')).not.toBeNull();
    expect(root.querySelectorAll('app-run-library').length).toBe(1);
    expect(q('.detail-empty')?.textContent?.trim()).toBe('Select a run to see its steps.');
    expect(q('.detail-empty')?.getAttribute('role')).toBe('status');
    expect(q('.right-panel')).toBeNull();
    expect(q('app-agent-stream')).toBeNull();
    expect(q('app-run-view')).toBeNull();
    expect(runs.get).not.toHaveBeenCalled();
    expect(q('.workspace-floating-bar-wrapper')).toBeNull();
  });

  it('passes team review restrictions to the unified RunView', async () => {
    await go(`/runs/${ID}?scope=everyone&review=1`);
    const view = harness.fixture.debugElement.query(By.directive(RunViewComponent)).componentInstance as RunViewComponent;
    expect(view.readOnly()).toBeTrue();
    expect(view.mode()).toBe('review');
    expect(view.runId()).toBe(ID);
    expect(q('.run-list-pane app-run-library.compact')).not.toBeNull();
  });

  it('shows the run viewer for the id in the URL on /runs/:id', async () => {
    await go(`/runs/${ID}`);
    expect(q('app-run-view')).not.toBeNull();
    expect(q('.detail-pane app-run-library, .detail-empty')).toBeNull();
    expect(q('.run-list-pane app-run-library.compact')).not.toBeNull();
    expect(runs.get).toHaveBeenCalledWith(ID);
  });

  it('updates the viewer when a reused /runs/:id route changes, then restores empty detail on /runs', async () => {
    await go(`/runs/${ID}?scope=everyone`);
    const workspace = harness.fixture.debugElement.query(By.directive(WorkspaceComponent)).componentInstance;
    await go('/runs/other-run');
    expect(harness.fixture.debugElement.query(By.directive(WorkspaceComponent)).componentInstance).toBe(workspace);
    const view = harness.fixture.debugElement.query(By.directive(RunViewComponent)).componentInstance as RunViewComponent;
    expect(view.runId()).toBe('other-run');
    expect(view.readOnly()).toBeFalse();
    expect(q('.workspace-container.has-run')).not.toBeNull();
    await go('/runs');
    expect(q('app-run-view, .workspace-container.has-run')).toBeNull();
    expect(q('.detail-empty')?.textContent?.trim()).toBe('Select a run to see its steps.');
  });

  it('has no decorative waves or glass in the live dock or in review mode', async () => {
    await go('/workspace');
    expect(q('.liquid-wave, .wave-glow-ambient, .dock-wave-container')).toBeNull();
    expect(getComputedStyle(q('.floating-dock-card')!).backdropFilter).toBe('none');
    await go('/runs');
    expect(q('.liquid-wave, .wave-glow-ambient, .dock-wave-container')).toBeNull();
  });

  describe('the list beside an open run (CHE-1278 F1)', () => {
    const listed = (id: string): RunSummary => ({
      session_id: id, prompt: `Run ${id.slice(0, 4)}`, status: 'completed', interrupt_reason: null, start_time: 1, end_time: 2,
      host_id: null, device_ref: { host_id: null, serial: 's' }, requested_by: null, pinned: false, recordings: []
    });
    const OTHER = '9a8b7c6d-5d7e-4a10-9c33-0e1f2a3b4c5d';
    const RETURN = { q: 'login', status: 'failed', scroll: '300' };

    beforeEach(() => {
      runs.list.and.returnValue(of({ runs: [listed(ID), listed(OTHER)], next_cursor: null, warnings: [] }));
    });

    it('keeps the filters and scroll that Back to runs returns to, and lists the same filtered runs', async () => {
      await go('/runs?q=login&status=failed&scroll=300');
      expect(runs.lastLibraryQuery()).toEqual(RETURN);
      await go(`/runs/${ID}`); // a row link from /runs carries no list state
      expect(runs.lastLibraryQuery()).toEqual(RETURN);
      expect(runs.list.calls.mostRecent().args[0]).toEqual(jasmine.objectContaining({ q: 'login', status: 'failed' }));
      const back = (q('app-run-view a.back-to-runs') ?? q('a.back-to-runs')) as HTMLAnchorElement | null;
      if (back) expect(back.getAttribute('href')).toBe('/runs?q=login&status=failed&scroll=300');
    });

    it('keeps them after choosing another run in the list beside the open one', async () => {
      await go('/runs?q=login&status=failed&scroll=300');
      await go(`/runs/${ID}`);
      const next = Array.from(root.querySelectorAll<HTMLAnchorElement>('.run-list-pane a.run-row')).find((a) => a.getAttribute('href')!.includes(OTHER))!;
      expect(next.getAttribute('href')).toContain('q=login');
      next.click();
      await go(`/runs/${OTHER}?q=login&status=failed`);
      expect(runs.lastLibraryQuery()).toEqual(RETURN);
    });
  });

  it('omits the mouse-only splitter in review mode', async () => {
    await go('/runs');
    expect(q('.resizer')).toBeNull();
  });

  it('opening history never selects a session or changes the device for the next run', async () => {
    localStorage.setItem('artemis.selected_device_serial', 'R58M123');
    await go('/runs');
    await go(`/runs/${ID}`);
    expect(TestBed.inject(AgentService).selectSession).not.toHaveBeenCalled();
    expect(localStorage.getItem('artemis.selected_device_serial')).toBe('R58M123');
  });

  it('prefills the dock with a prompt handed over from the viewer', async () => {
    await go('/workspace', { draftPrompt: 'Log in and open settings' });
    expect((q('textarea.dock-textarea') as HTMLTextAreaElement).value).toBe('Log in and open settings');
  });

  for (const connected of [false, true]) {
    it(`shows one interrupted banner with a ${connected ? 'connected' : 'disconnected'} phone`, async () => {
      const prompt = 'Open Settings';
      liveSession.set({ session_id: ID, initial_goal: prompt, start_time: 1, status: 'interrupted' });
      if (connected) target.set({ serial: 'phone-1' });
      runs.get.and.returnValue(of<RunSummary>({
        session_id: ID, prompt, status: 'interrupted', interrupt_reason: 'device_offline',
        start_time: 1, end_time: 2, host_id: null, device_ref: null, requested_by: null,
        pinned: false, recordings: []
      }));
      runs.steps.and.returnValue(of([]));
      await go('/workspace');

      expect(TestBed.inject(WorkspacePhoneService).runInterrupted()).toBeTrue();
      const banners = Array.from(root.querySelectorAll<HTMLElement>('[role="status"]'))
        .filter((element) => element.textContent?.includes('Run interrupted before the first step.'));
      expect(banners.length).toBe(1);
      expect(banners[0].closest('app-run-view')).not.toBeNull();
      const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>('button'));
      const starts = buttons.filter((element) => element.textContent?.includes('Start new run with this prompt'));
      expect(starts.length).toBe(1);
      starts[0].click();
      harness.fixture.detectChanges();
      await harness.fixture.whenStable();
      harness.fixture.detectChanges();
      expect((q('textarea.dock-textarea') as HTMLTextAreaElement).value).toBe(prompt);

      const reconnects = buttons.filter((element) => element.textContent?.includes('Reconnect phone'));
      expect(reconnects.length).toBe(connected ? 0 : 1);
      if (!connected) {
        expect(reconnects[0].tagName).toBe('BUTTON');
        expect(reconnects[0].disabled).toBeFalse();
        reconnects[0].click();
        expect(reconnect).toHaveBeenCalledTimes(1);
      }
    });
  }
});
