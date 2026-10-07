import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { fakeAsync, TestBed, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { AgentService } from '../../services/agent.service';
import { RunViewComponent } from '../../components/run-view/run-view.component';
import { ChatInterfaceComponent } from '../../components/chat-interface/chat-interface.component';
import { WorkspaceComponent } from './workspace.component';

describe('WorkspaceComponent error lifetime', () => {
  let agentService: {
    whatsNewPromptDraft: ReturnType<typeof signal<boolean>>;
    updateWhatsNewErrorVisibility: (owner: symbol, visible: boolean) => void;
    isCurrentSessionRunning: () => boolean;
    currentSession: () => null;
    currentSessionId: () => null;
    currentStartupProgress: () => [];
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
      currentStartupProgress: () => [],
      runTask: jasmine.createSpy('runTask').and.returnValue(of({})),
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask')
    };

    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [provideRouter([]), { provide: AgentService, useValue: agentService }],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [RunViewComponent, ChatInterfaceComponent] },
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

  for (const width of [1150, 1024, 390]) {
    it(`keeps the task-switcher and notes panel visible at ${width}px`, () => {
      const frame = document.createElement('iframe');
      frame.style.width = `${width}px`;
      frame.style.height = '768px';
      document.body.appendChild(frame);
      const fixture = TestBed.createComponent(WorkspaceComponent);
      try {
        fixture.detectChanges();
        const frameDocument = frame.contentDocument!;
        for (const style of Array.from(document.head.querySelectorAll('style'))) {
          frameDocument.head.appendChild(style.cloneNode(true));
        }
        frameDocument.body.appendChild(fixture.nativeElement);
        const panel = frameDocument.querySelector<HTMLElement>('.right-panel')!;
        const left = frameDocument.querySelector<HTMLElement>('.left-panel')!;
        expect(frame.contentWindow!.getComputedStyle(panel).display).not.toBe('none');
        expect(panel.querySelector('app-chat-interface')).not.toBeNull();
        expect(panel.getBoundingClientRect().top).toBeGreaterThanOrEqual(left.getBoundingClientRect().bottom);
        expect(panel.getBoundingClientRect().height).toBeGreaterThan(0);
        expect(panel.getBoundingClientRect().width).toBeLessThanOrEqual(width);
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
