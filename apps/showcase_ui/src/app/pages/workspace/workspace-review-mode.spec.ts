import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { provideLocationMocks } from '@angular/common/testing';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { NEVER, of } from 'rxjs';
import { routes } from '../../app.routes';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { AdminConfigService } from '../../services/admin-config.service';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { RunsService } from '../../services/runs.service';
import { WorkspaceComponent } from './workspace.component';

const ID = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';

describe('Workspace review mode', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let harness: RouterTestingHarness;
  let root: HTMLElement;
  const q = (selector: string) => root.querySelector(selector);

  beforeEach(async () => {
    runs = jasmine.createSpyObj<RunsService>('RunsService', ['list', 'get', 'steps', 'video'], {
      lastLibraryQuery: signal<Record<string, string>>({}), viewPosition: signal(null)
    });
    runs.list.and.returnValue(NEVER);
    runs.get.and.returnValue(NEVER);
    runs.steps.and.returnValue(NEVER);
    runs.video.and.returnValue(NEVER);
    const hosts = jasmine.createSpyObj<HostsService>('HostsService', ['list']);
    hosts.list.and.returnValue(NEVER);
    const admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    admin.getIdentity.and.returnValue(of({ email: null, admin: false, auth_mode: 'open', reason: null }));
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => undefined,
      isCurrentSessionRunning: () => false,
      currentSession: () => null,
      currentSessionId: () => null,
      currentStartupProgress: () => [],
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
        { provide: HostsService, useValue: hosts },
        { provide: AdminConfigService, useValue: admin }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [ChatInterfaceComponent, FloatingVideoPlayerComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
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

  it('shows the shared RunView, new-task box and splitter on /workspace', async () => {
    await go('/workspace');
    expect(q('app-run-view')).not.toBeNull();
    expect(q('textarea.dock-textarea')).not.toBeNull();
    expect(q('.workspace-container.review-mode')).toBeNull();
    expect(q('.resizer')).not.toBeNull();
    expect(q('app-run-library')).toBeNull();
  });

  it('shows the run library without the live new-task box on /runs', async () => {
    await go('/runs');
    expect(q('.workspace-container.review-mode')).not.toBeNull();
    expect(q('app-run-library')).not.toBeNull();
    expect(q('app-agent-stream')).toBeNull();
    expect(q('.workspace-floating-bar-wrapper')).toBeNull();
  });

  it('shows the run viewer for the id in the URL on /runs/:id', async () => {
    await go(`/runs/${ID}`);
    expect(q('app-run-view')).not.toBeNull();
    expect(q('app-run-library')).toBeNull();
    expect(runs.get).toHaveBeenCalledWith(ID);
  });

  it('has no looping decorative motion in review mode, unlike the live workspace', async () => {
    await go('/workspace');
    expect(getComputedStyle(q('.liquid-wave')!).animationName).not.toBe('none');
    await go('/runs');
    for (const el of Array.from(root.querySelectorAll('.liquid-wave, .wave-glow-ambient'))) {
      expect(getComputedStyle(el).animationName).toBe('none');
    }
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
});
