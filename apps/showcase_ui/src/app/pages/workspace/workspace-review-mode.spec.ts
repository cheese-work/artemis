import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { provideLocationMocks } from '@angular/common/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { NEVER, of } from 'rxjs';
import { routes } from '../../app.routes';
import { RunViewComponent } from '../../components/run-view/run-view.component';
import { RunLibraryComponent } from '../../components/run-library/run-library.component';
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
  let sessions: ReturnType<typeof signal<Session[]>>;
  let target: ReturnType<typeof signal<RunTarget | null>>;
  let reconnect: jasmine.Spy;
  const q = (selector: string) => root.querySelector(selector);

  beforeEach(async () => {
    liveSession = signal<Session | null>(null);
    sessions = signal<Session[]>([]);
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
      sessions,
      currentSessionId: () => liveSession()?.session_id ?? null,
      currentStartupProgress: () => [],
      runningSessionId: () => null,
      agentStatus: () => 'idle',
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
    frame.style.border = '0';
    document.body.appendChild(frame);
    const workspace = q('app-workspace')!;
    const parent = workspace.parentNode!;
    const next = workspace.nextSibling;
    try {
      const frameDocument = frame.contentDocument!;
      expect(frame.contentWindow!.innerWidth).toBe(width);
      frameDocument.body.style.cssText = 'margin: 0; height: 100vh';
      const tokens = getComputedStyle(document.documentElement);
      for (const property of Array.from(tokens)) {
        if (property.startsWith('--')) frameDocument.documentElement.style.setProperty(property, tokens.getPropertyValue(property));
      }
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
          } else if (route === '/runs') {
            const navClearance = parseFloat(frame.contentWindow!.getComputedStyle(frameDocument.documentElement).getPropertyValue('--nav-clearance')) || 88;
            const firstControl = list.querySelector<HTMLButtonElement>('.owner-tabs button')!;
            expect(firstControl.getBoundingClientRect().top).toBeGreaterThanOrEqual(navClearance);
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
