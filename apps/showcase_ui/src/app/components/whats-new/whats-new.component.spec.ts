import { HttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AgentService } from '../../services/agent.service';
import { WhatsNewComponent, WHATS_NEW_LAST_SEEN_KEY } from './whats-new.component';

describe('WhatsNewComponent', () => {
  const entries = [
    { id: 'second', date: '2026-10-03', title: 'Second update', body: 'Second body' },
    { id: 'first', date: '2026-10-02', title: 'First update', body: 'First body' }
  ];

  let agentService: {
    agentStatus: ReturnType<typeof signal<string>>;
    activeTasks: ReturnType<typeof signal<any[]>>;
    hasFetchedStatus: ReturnType<typeof signal<boolean>>;
    whatsNewHasUpdates: ReturnType<typeof signal<boolean>>;
    whatsNewHasUnread: ReturnType<typeof signal<boolean>>;
    whatsNewPromptDraft: ReturnType<typeof signal<boolean>>;
    whatsNewErrorVisible: ReturnType<typeof signal<boolean>>;
    whatsNewAcceptedRunHandoffs: ReturnType<typeof signal<number>>;
  };
  let response: unknown;

  beforeEach(() => {
    localStorage.removeItem(WHATS_NEW_LAST_SEEN_KEY);
    response = entries;
    agentService = {
      agentStatus: signal('idle'),
      activeTasks: signal([]),
      hasFetchedStatus: signal(true),
      whatsNewHasUpdates: signal(false),
      whatsNewHasUnread: signal(false),
      whatsNewPromptDraft: signal(false),
      whatsNewErrorVisible: signal(false),
      whatsNewAcceptedRunHandoffs: signal(0)
    };

    TestBed.configureTestingModule({
      imports: [WhatsNewComponent],
      providers: [
        { provide: HttpClient, useValue: { get: () => of(response) } },
        { provide: AgentService, useValue: agentService }
      ]
    });
  });

  afterEach(() => localStorage.removeItem(WHATS_NEW_LAST_SEEN_KEY));

  it('opens once on first visit when new entries exist', async () => {
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBeTrue();
    expect(fixture.nativeElement.textContent).toContain('First update');
    expect(dialog.querySelector('[autofocus]') === document.activeElement).toBeTrue();
  });

  it('does not open automatically when the latest entry is already seen', async () => {
    localStorage.setItem(WHATS_NEW_LAST_SEEN_KEY, 'second');
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect((fixture.nativeElement.querySelector('dialog') as HTMLDialogElement).open).toBeFalse();
  });

  it('defers auto-open during a run and opens after the run ends', async () => {
    agentService.agentStatus.set('running');
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBeFalse();

    agentService.agentStatus.set('idle');
    fixture.detectChanges();
    await fixture.whenStable();

    expect(dialog.open).toBeTrue();
  });

  it('keeps auto-open suppressed between accepted submission and active status', async () => {
    agentService.whatsNewAcceptedRunHandoffs.set(1);
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBeFalse();

    agentService.agentStatus.set('running');
    agentService.activeTasks.set([{ session_id: 'accepted-run', status: 'running' }]);
    agentService.whatsNewAcceptedRunHandoffs.set(0);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(dialog.open).toBeFalse();
  });

  it('defers auto-open while a prompt draft exists and opens after it is cleared', async () => {
    agentService.whatsNewPromptDraft.set(true);
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBeFalse();
    agentService.whatsNewPromptDraft.set(false);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(dialog.open).toBeTrue();
  });

  it('defers auto-open while a submission error is visible', async () => {
    agentService.whatsNewErrorVisible.set(true);
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBeFalse();
    agentService.whatsNewErrorVisible.set(false);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(dialog.open).toBeTrue();
  });

  it('keeps the nav hidden when the shipped update list is empty', async () => {
    response = [];
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    expect(agentService.whatsNewHasUpdates()).toBeFalse();
    expect(agentService.whatsNewHasUnread()).toBeFalse();
    expect((fixture.nativeElement.querySelector('dialog') as HTMLDialogElement).open).toBeFalse();
  });

  it('stores the latest entry id after closing the side sheet', async () => {
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    const component = fixture.componentInstance;
    const dialog = fixture.nativeElement.querySelector('dialog') as HTMLDialogElement;
    const closed = new Promise<void>(resolve => dialog.addEventListener('close', () => resolve(), { once: true }));

    component.close();
    await closed;

    expect(localStorage.getItem(WHATS_NEW_LAST_SEEN_KEY)).toBe('second');
    expect(agentService.whatsNewHasUnread()).toBeFalse();
  });

  it('keeps Tab and Shift+Tab inside the open side sheet', async () => {
    const fixture = TestBed.createComponent(WhatsNewComponent);
    fixture.detectChanges();
    await fixture.whenStable();

    const closeButton = fixture.nativeElement.querySelector('.close-button') as HTMLButtonElement;
    const doneButton = fixture.nativeElement.querySelector('.done-button') as HTMLButtonElement;
    closeButton.focus();

    const shiftTab = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(shiftTab);
    expect(shiftTab.defaultPrevented).toBeTrue();
    expect(document.activeElement).toBe(doneButton);

    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    document.dispatchEvent(tab);
    expect(tab.defaultPrevented).toBeTrue();
    expect(document.activeElement).toBe(closeButton);
  });
});
