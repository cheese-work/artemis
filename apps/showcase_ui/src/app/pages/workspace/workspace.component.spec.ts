import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, fakeAsync, TestBed, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { RunTarget } from '../../core/models/run-target.model';
import { AgentService } from '../../services/agent.service';
import { RunViewComponent } from '../../components/run-view/run-view.component';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { InterruptedBannerComponent } from '../../components/interrupted-banner/interrupted-banner.component';
import { RunLibraryComponent } from '../../components/run-library/run-library.component';
import { WorkspaceComponent } from './workspace.component';

describe('WorkspaceComponent error lifetime', () => {
  let agentService: {
    whatsNewPromptDraft: ReturnType<typeof signal<boolean>>;
    updateWhatsNewErrorVisibility: (owner: symbol, visible: boolean) => void;
    isCurrentSessionRunning: () => boolean;
    currentSession: () => null;
    currentSessionId: () => null;
    sessions: () => [];
    currentStartupProgress: () => [];
    runningSessionId: () => null;
    agentStatus: () => string;
    runTask: jasmine.Spy;
    fetchStatus: jasmine.Spy;
    stopTask: jasmine.Spy;
  };
  let errorOwners: Set<symbol>;

  beforeEach(async () => {
    errorOwners = new Set<symbol>();
    agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: (owner, visible) => {
        if (visible) errorOwners.add(owner);
        else errorOwners.delete(owner);
      },
      isCurrentSessionRunning: () => false,
      currentSession: () => null,
      currentSessionId: () => null,
      sessions: () => [],
      currentStartupProgress: () => [],
      runningSessionId: () => null,
      agentStatus: () => 'idle',
      runTask: jasmine.createSpy('runTask').and.returnValue(of({})),
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask')
    };

    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        { provide: WorkspacePhoneService, useValue: { target: () => null, requestPicker: () => undefined } }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [RunViewComponent, RunLibraryComponent, InterruptedBannerComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
  });

  it('uses the first prompt line without markdown in the Stop button title', () => {
    spyOn(TestBed.inject(AgentService), 'currentSession').and.returnValue({
      session_id: 'run-title', initial_goal: '## **Open** _Settings_\nCheck every toggle.', start_time: 1
    });
    const fixture = TestBed.createComponent(WorkspaceComponent);
    expect(fixture.componentInstance.stopButtonTitle()).toBe('Stop current task: "Open Settings"');
  });

  it('cancels a destroyed route error timer without releasing a newer error', fakeAsync(() => {
    const olderFixture = TestBed.createComponent(WorkspaceComponent);
    olderFixture.detectChanges();
    olderFixture.componentInstance.setErrorMessage('Older submission failed');
    expect(errorOwners.size).toBe(1);
    olderFixture.destroy();
    expect(errorOwners.size).toBe(0);

    tick(2500);
    const newerFixture = TestBed.createComponent(WorkspaceComponent);
    newerFixture.detectChanges();
    newerFixture.componentInstance.setErrorMessage('Newer submission failed');

    tick(2500);
    expect(newerFixture.componentInstance.errorMessage()).toBe('Newer submission failed');
    expect(errorOwners.size).toBe(1);

    tick(2500);
    expect(newerFixture.componentInstance.errorMessage()).toBeNull();
    expect(errorOwners.size).toBe(0);
  }));

  for (const width of [1440, 1200, 1024, 1023, 800, 390]) {
    it(`uses a fixed run list or one detail pane at ${width}px`, () => {
      const frame = document.createElement('iframe');
      frame.style.width = `${width}px`;
      frame.style.height = '768px';
      frame.style.border = '0';
      document.body.appendChild(frame);
      const fixture = TestBed.createComponent(WorkspaceComponent);
      try {
        fixture.detectChanges();
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
        frameDocument.body.appendChild(fixture.nativeElement);
        const list = frameDocument.querySelector<HTMLElement>('.run-list-pane')!;
        const detail = frameDocument.querySelector<HTMLElement>('.detail-pane')!;
        const navigation = frameDocument.querySelector<HTMLAnchorElement>('.pane-navigation')!;
        expect(list.querySelector('app-run-library')).not.toBeNull();
        expect(frameDocument.querySelector('.right-panel, .resizer, app-chat-interface')).toBeNull();
        expect(detail.getBoundingClientRect().height).toBeGreaterThan(0);
        if (width >= 1024) {
          expect(list.getBoundingClientRect().width).toBe(360);
          expect(list.getBoundingClientRect().right).toBeCloseTo(detail.getBoundingClientRect().left, 0);
          expect(list.getBoundingClientRect().top).toBe(detail.getBoundingClientRect().top);
          expect(frame.contentWindow!.getComputedStyle(navigation).display).toBe('none');
        } else {
          expect(frame.contentWindow!.getComputedStyle(list).display).toBe('none');
          expect(frame.contentWindow!.getComputedStyle(navigation).display).not.toBe('none');
          expect(navigation.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
          expect(navigation.getBoundingClientRect().width).toBeGreaterThanOrEqual(44);
          expect(navigation.getAttribute('href')).toBe('/runs');
        }
        expect(detail.getBoundingClientRect().right).toBeLessThanOrEqual(width);
      } finally {
        fixture.destroy();
        frame.remove();
      }
    });
  }

  it('does not mount a second floating video controller beside RunView evidence', () => {
    const fixture = TestBed.createComponent(WorkspaceComponent);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-floating-video-player')).toBeNull();
    fixture.destroy();
  });
});

describe('WorkspaceComponent phone binding', () => {
  let runTask: jasmine.Spy;
  let phone: { target: ReturnType<typeof signal<RunTarget | null>>; requestPicker: jasmine.Spy };
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;

  const sendButton = () => (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('button.btn-send')!;

  beforeEach(async () => {
    runTask = jasmine.createSpy('runTask').and.returnValue(of({}));
    phone = { target: signal<RunTarget | null>(null), requestPicker: jasmine.createSpy('requestPicker') };
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => undefined,
      isCurrentSessionRunning: () => false,
      currentSession: () => null,
      currentSessionId: () => null,
      sessions: () => [],
      currentStartupProgress: () => [],
      runningSessionId: () => null,
      agentStatus: () => 'idle',
      runTask,
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask')
    };
    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        { provide: WorkspacePhoneService, useValue: phone }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [RunViewComponent, RunLibraryComponent, InterruptedBannerComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
    fixture = TestBed.createComponent(WorkspaceComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('blocks Run with no phone: says why, keeps the prompt, opens the picker, sends nothing', async () => {
    component.taskInput = 'open settings';
    fixture.detectChanges();
    expect(sendButton().getAttribute('aria-disabled')).toBe('true');
    expect(sendButton().title).toBe('Connect a phone first');

    await component.submitTask();

    expect(runTask).not.toHaveBeenCalled();
    expect(phone.requestPicker).toHaveBeenCalledTimes(1);
    expect(component.taskInput).toBe('open settings');
  });

  it('blocks the Enter key the same way', () => {
    component.taskInput = 'open settings';
    component.onKeyDown(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(runTask).not.toHaveBeenCalled();
    expect(phone.requestPicker).toHaveBeenCalled();
  });

  it('sends the run to exactly the chosen serial and its bridge session', async () => {
    phone.target.set({ serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' });
    component.taskInput = 'open settings';
    fixture.detectChanges();
    expect(sendButton().getAttribute('aria-disabled')).toBeNull();

    await component.submitTask();

    expect(runTask.calls.mostRecent().args[6]).toEqual({ serial: '127.0.0.1:41003', bridgeSessionId: 'bridge-9' });
    expect(phone.requestPicker).not.toHaveBeenCalled();
  });

  for (const code of ['device_offline', 'bridge_session_unavailable', 'bridge_queue_binding_unavailable']) {
    it(`keeps the prompt and opens the picker when the server returns ${code}`, async () => {
      phone.target.set({ serial: 's', bridgeSessionId: 'b' });
      runTask.and.returnValue(throwError(() => ({ status: 409, error: { code, detail: 'x' } })));
      component.taskInput = 'open settings';

      await component.submitTask();

      expect(component.errorMessage()).toBe('Your phone is not connected. Connect it again to run.');
      expect(phone.requestPicker).toHaveBeenCalled();
      expect(component.taskInput).toBe('open settings');
      expect(runTask).toHaveBeenCalledTimes(1);
    });
  }

  it('has no phone chip in the Prompt Dock: the chip lives in the top bar (CHE-1143, OCR F6)', () => {
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-workspace-device-chip')).toBeNull();
    expect(el.querySelector('.workspace-floating-bar-wrapper .dock-chip')).toBeNull();
  });

  it('starts a new run from the interrupted run’s prompt', () => {
    component.startNewRunFrom('Open Settings');
    expect(component.taskInput).toBe('Open Settings');
  });
});
