import { CUSTOM_ELEMENTS_SCHEMA, signal } from '@angular/core';
import { ComponentFixture, fakeAsync, TestBed, tick } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { RunTarget } from '../../core/models/run-target.model';
import { AgentService } from '../../services/agent.service';
import { BrowserStorageService } from '../../services/browser-storage.service';
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
    sessions: () => [];
    currentSession: () => null;
    currentSessionId: () => null;
    sessions: () => [];
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
      sessions: () => [],
      currentSession: () => null,
      currentSessionId: () => null,
      sessions: () => [],
      currentStartupProgress: () => [],
      runTask: jasmine.createSpy('runTask').and.returnValue(of({})),
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask: jasmine.createSpy('stopTask')
    };

    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        { provide: WorkspacePhoneService, useValue: { target: () => null, view: () => ({ text: 'No phone' }), requestPicker: () => undefined } }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [RunViewComponent, RunLibraryComponent, InterruptedBannerComponent] },
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
  let phone: {
    target: ReturnType<typeof signal<RunTarget | null>>;
    view: () => { text: string };
    requestPicker: jasmine.Spy;
  };
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;

  const sendButton = () => (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('button.btn-send')!;

  beforeEach(async () => {
    runTask = jasmine.createSpy('runTask').and.returnValue(of({}));
    phone = {
      target: signal<RunTarget | null>(null),
      view: () => ({ text: 'No phone' }),
      requestPicker: jasmine.createSpy('requestPicker')
    };
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => undefined,
      isCurrentSessionRunning: () => false,
      sessions: () => [],
      currentSession: () => null,
      currentSessionId: () => null,
      sessions: () => [],
      currentStartupProgress: () => [],
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
    expect(el.querySelector('.composer .dock-chip')).toBeNull();
  });

  it('starts a new run from the interrupted run’s prompt', () => {
    component.startNewRunFrom('Open Settings');
    expect(component.taskInput).toBe('Open Settings');
  });
});

describe('WorkspaceComponent pinned composer (CHE-1508)', () => {
  let runTask: jasmine.Spy;
  let running: ReturnType<typeof signal<boolean>>;
  let sessions: ReturnType<typeof signal<{ session_id: string; status: string }[]>>;
  let target: ReturnType<typeof signal<RunTarget | null>>;
  let fixture: ComponentFixture<WorkspaceComponent>;
  let component: WorkspaceComponent;
  let el: HTMLElement;
  let stopTask: jasmine.Spy;

  const png = (name: string) => new File([new Uint8Array([137, 80, 78, 71])], name, { type: 'image/png' });

  beforeEach(async () => {
    runTask = jasmine.createSpy('runTask').and.returnValue(of({}));
    running = signal(false);
    sessions = signal<{ session_id: string; status: string }[]>([]);
    target = signal<RunTarget | null>({ serial: '1A2B3C4D5E', bridgeSessionId: 'bridge-1' });
    stopTask = jasmine.createSpy('stopTask');
    const agentService = {
      whatsNewPromptDraft: signal(false),
      updateWhatsNewErrorVisibility: () => undefined,
      isCurrentSessionRunning: running,
      sessions,
      currentSession: () => null,
      currentSessionId: () => 'run-live-1',
      sessionLogs: () => [],
      currentStartupProgress: () => [],
      runTask,
      fetchStatus: jasmine.createSpy('fetchStatus'),
      stopTask
    };
    await TestBed.configureTestingModule({
      imports: [WorkspaceComponent],
      providers: [
        provideRouter([]),
        { provide: AgentService, useValue: agentService },
        {
          provide: WorkspacePhoneService,
          useValue: { target, view: () => ({ text: 'Pixel 6 Pro · …4D5E' }), requestPicker: jasmine.createSpy('requestPicker') }
        }
      ],
      schemas: [CUSTOM_ELEMENTS_SCHEMA]
    }).overrideComponent(WorkspaceComponent, {
      remove: { imports: [RunViewComponent, RunLibraryComponent, InterruptedBannerComponent] },
      add: { schemas: [CUSTOM_ELEMENTS_SCHEMA] }
    }).compileComponents();
    fixture = TestBed.createComponent(WorkspaceComponent);
    component = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  // Flash/Pro choices persist in browser storage; forget them so other suites start on Flash.
  afterEach(() => {
    fixture.destroy();
    TestBed.inject(BrowserStorageService).removeItem('artemis_selected_profile');
  });

  const hint = () => el.querySelector<HTMLElement>('.composer-hint')!.textContent!.replace(/\s+/g, ' ').trim();
  const textarea = () => el.querySelector<HTMLTextAreaElement>('.composer textarea')!;

  it('pins one composer at the bottom of the detail pane, after the run surface', () => {
    const detail = el.querySelector('.detail-pane')!;
    const composer = detail.querySelector('.composer')!;
    expect(composer).not.toBeNull();
    expect(detail.lastElementChild).toBe(composer);
    expect(el.querySelectorAll('.composer').length).toBe(1);
  });

  it('names the target phone in the hint line and sends to it', async () => {
    expect(hint()).toBe('Runs on Pixel 6 Pro · …4D5E');
    component.taskInput = 'open settings';
    fixture.detectChanges();
    el.querySelector<HTMLButtonElement>('button.btn-send')!.click();
    await fixture.whenStable();
    expect(runTask).toHaveBeenCalledTimes(1);
    expect(runTask.calls.mostRecent().args[0]).toBe('open settings');
    expect(component.taskInput).toBe('');
  });

  it('queues during a run: the placeholder says so and the hint counts the queue', () => {
    running.set(true);
    sessions.set([
      { session_id: 'a', status: 'running' },
      { session_id: 'b', status: 'pending' }
    ]);
    fixture.detectChanges();
    expect(textarea().placeholder).toBe('Describe the next task. It queues after this run.');
    expect(hint()).toBe('Runs on Pixel 6 Pro · …4D5E · 1 run already queued');

    sessions.update((list) => [...list, { session_id: 'c', status: 'pending' }]);
    fixture.detectChanges();
    expect(hint()).toBe('Runs on Pixel 6 Pro · …4D5E · 2 runs already queued');
  });

  it('says how to get a phone when none is chosen', () => {
    target.set(null);
    fixture.detectChanges();
    expect(hint()).toBe('No phone yet. Send opens the phone picker.');
  });

  it('attaches an image as a preview and removes it again', () => {
    const revoke = spyOn(URL, 'revokeObjectURL').and.callThrough();
    component.addImages([png('home.png')]);
    fixture.detectChanges();
    const previews = el.querySelectorAll('.composer .attached-image');
    expect(previews.length).toBe(1);
    expect(previews[0].querySelector('img')!.alt).toBe('Preview of home.png');

    el.querySelector<HTMLButtonElement>('button.btn-remove-image')!.click();
    fixture.detectChanges();
    expect(el.querySelectorAll('.composer .attached-image').length).toBe(0);
    expect(component.attachedImages().length).toBe(0);
    expect(revoke).toHaveBeenCalledTimes(1);
  });

  it('toggles Flash and Pro, marks the pressed segment and remembers it', () => {
    const [flash, pro] = Array.from(el.querySelectorAll<HTMLButtonElement>('.composer .segment'));
    expect(flash.getAttribute('aria-pressed')).toBe('true');
    expect(pro.getAttribute('aria-pressed')).toBe('false');

    pro.click();
    fixture.detectChanges();
    expect(component.selectedProfile()).toBe('pro');
    expect(pro.getAttribute('aria-pressed')).toBe('true');
    expect(flash.getAttribute('aria-pressed')).toBe('false');

    flash.click();
    fixture.detectChanges();
    expect(component.selectedProfile()).toBe('flash');
  });

  it('places the interrupted banner and the error toast above the input', () => {
    component.setErrorMessage('The runner is busy.');
    fixture.detectChanges();
    const composer = el.querySelector('.composer')!;
    const banner = composer.querySelector('app-interrupted-banner')!;
    const toast = composer.querySelector('.composer-toast')!;
    const input = textarea();
    expect(banner).not.toBeNull();
    expect(toast.textContent).toContain('The runner is busy.');
    expect(banner.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(toast.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('queues a task during a run: one submission with the prompt, profile and chosen phone', async () => {
    running.set(true);
    const chosen: RunTarget = { serial: 'R58M123ABC', bridgeSessionId: 'bridge-7' };
    target.set(chosen);
    fixture.detectChanges();

    const input = textarea();
    input.value = 'Check dark mode on Home';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    el.querySelectorAll<HTMLButtonElement>('.composer .segment')[1].click();
    fixture.detectChanges();

    const send = el.querySelector<HTMLButtonElement>('.composer button.btn-send')!;
    expect(send.getAttribute('aria-label')).toBe('Queue task');
    send.click();
    await fixture.whenStable();

    expect(runTask).toHaveBeenCalledTimes(1);
    const args = runTask.calls.mostRecent().args;
    expect(args[0]).toBe('Check dark mode on Home');
    expect(args[1]).toBe('pro');
    expect(args[5]).toBeUndefined();
    expect(args[6]).toEqual(chosen);
  });

  it('has no Stop run while idle', () => {
    expect(el.querySelector('.detail-header')).toBeNull();
    expect(el.querySelector('[aria-label="Stop run"]')).toBeNull();
  });

  it('puts Stop run in the detail header during a run, never in the composer', () => {
    running.set(true);
    fixture.detectChanges();
    expect(el.querySelector('.composer .btn-stop, .composer [aria-label="Stop run"], .composer .btn-stop-run')).toBeNull();

    const header = el.querySelector('.detail-pane > .detail-header')!;
    const stop = header.querySelector<HTMLButtonElement>('button.btn-stop-run')!;
    expect(stop.getAttribute('aria-label')).toBe('Stop run');
    expect(stop.textContent).toContain('Stop run');
    expect(header.compareDocumentPosition(el.querySelector('.run-surface')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    stop.click();
    expect(stopTask).toHaveBeenCalledOnceWith('run-live-1', false);
  });

  for (const width of [1440, 390]) {
    it(`gives every composer control a 44 px hit box at ${width}px`, () => {
      running.set(true);
      component.taskInput = 'open settings';
      component.addImages([png('a.png'), png('b.png')]);
      component.setErrorMessage('The runner is busy.');
      fixture.detectChanges();

      const frame = document.createElement('iframe');
      frame.style.cssText = `width: ${width}px; height: 844px; border: 0`;
      document.body.appendChild(frame);
      try {
        const doc = frame.contentDocument!;
        doc.body.style.cssText = 'margin: 0; height: 100vh';
        const tokens = getComputedStyle(document.documentElement);
        for (const property of Array.from(tokens)) {
          if (property.startsWith('--')) doc.documentElement.style.setProperty(property, tokens.getPropertyValue(property));
        }
        for (const style of Array.from(document.head.querySelectorAll('style'))) doc.head.appendChild(style.cloneNode(true));
        doc.body.appendChild(el);

        expect(doc.querySelector('.composer .btn-stop, .composer .btn-stop-run')).toBeNull();
        const controls = Array.from(doc.querySelectorAll<HTMLElement>('.composer button, .detail-header button'));
        const names = controls.map((control) => control.className);
        for (const cls of ['btn-send', 'btn-attach', 'btn-stop-run', 'btn-remove-image', 'btn-toast-close', 'segment']) {
          expect(names.some((name) => name.includes(cls))).withContext(cls).toBeTrue();
        }
        const boxes = controls.map((control) => ({ name: control.className, box: control.getBoundingClientRect() }));
        for (const { name, box } of boxes) {
          expect(box.width).withContext(`${name} width`).toBeGreaterThanOrEqual(44);
          expect(box.height).withContext(`${name} height`).toBeGreaterThanOrEqual(44);
          expect(box.right).withContext(`${name} inside the viewport`).toBeLessThanOrEqual(width);
        }
        // Hit boxes of neighbours do not overlap (spec section 3).
        for (let i = 0; i < boxes.length; i++) {
          for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i].box;
            const b = boxes[j].box;
            const overlap = a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
            expect(overlap).withContext(`${boxes[i].name} / ${boxes[j].name}`).toBeFalse();
          }
        }
        // The visual stays smaller than the box: segments draw a 36 px face inside 44 px.
        const face = doc.querySelector<HTMLElement>('.composer .segment .segment-face')!;
        expect(face.getBoundingClientRect().height).toBe(36);
        const stopFace = doc.querySelector<HTMLElement>('.detail-header .btn-stop-run .stop-face')!;
        expect(stopFace.getBoundingClientRect().height).toBe(36);
      } finally {
        frame.remove();
      }
    });
  }
});
