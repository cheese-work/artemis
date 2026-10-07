import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, fakeAsync, TestBed, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { RunTarget } from '../../core/models/run-target.model';
import { AgentService } from '../../services/agent.service';
import { WorkspacePhoneService } from '../../services/workspace-phone.service';
import { InterruptedBannerComponent } from '../../components/interrupted-banner/interrupted-banner.component';
import { WorkspaceDeviceChipComponent } from '../../components/workspace-device-chip/workspace-device-chip.component';
import { AgentStreamComponent } from '../../components/agent-stream/agent-stream.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { FloatingVideoPlayerComponent } from '../../components/floating-video-player/floating-video-player.component';
import { WorkspaceComponent } from './workspace.component';

describe('WorkspaceComponent error lifetime', () => {
  let agentService: {
    whatsNewPromptDraft: ReturnType<typeof signal<boolean>>;
    updateWhatsNewErrorVisibility: (owner: symbol, visible: boolean) => void;
    isCurrentSessionRunning: () => boolean;
    currentSession: () => null;
    currentSessionId: () => null;
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
      remove: { imports: [AgentStreamComponent, ChatInterfaceComponent, FloatingVideoPlayerComponent, InterruptedBannerComponent, WorkspaceDeviceChipComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
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
      remove: { imports: [AgentStreamComponent, ChatInterfaceComponent, FloatingVideoPlayerComponent, InterruptedBannerComponent, WorkspaceDeviceChipComponent] },
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

  it('keeps the prompt and opens the picker when the server says the phone is gone', async () => {
    phone.target.set({ serial: 's', bridgeSessionId: 'b' });
    runTask.and.returnValue(throwError(() => ({ status: 409, error: { code: 'device_offline', detail: 'x' } })));
    component.taskInput = 'open settings';

    await component.submitTask();

    expect(component.errorMessage()).toBe('Your phone is not connected. Connect it again to run.');
    expect(phone.requestPicker).toHaveBeenCalled();
    expect(component.taskInput).toBe('open settings');
    expect(runTask).toHaveBeenCalledTimes(1);
  });

  it('starts a new run from the interrupted run’s prompt', () => {
    component.startNewRunFrom('Open Settings');
    expect(component.taskInput).toBe('Open Settings');
  });
});
