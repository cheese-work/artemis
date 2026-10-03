import { HttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { AgentService } from '../../services/agent.service';
import { WhatsNewComponent, WHATS_NEW_LAST_SEEN_KEY } from './whats-new.component';

describe('WhatsNewComponent', () => {
  const entries = [
    { id: 'first', date: '2026-10-02', title: 'First update', body: 'First body' },
    { id: 'second', date: '2026-10-03', title: 'Second update', body: 'Second body' }
  ];

  let agentService: {
    agentStatus: ReturnType<typeof signal<string>>;
    activeTasks: ReturnType<typeof signal<any[]>>;
    hasFetchedStatus: ReturnType<typeof signal<boolean>>;
  };

  beforeEach(() => {
    localStorage.removeItem(WHATS_NEW_LAST_SEEN_KEY);
    agentService = {
      agentStatus: signal('idle'),
      activeTasks: signal([]),
      hasFetchedStatus: signal(true)
    };

    TestBed.configureTestingModule({
      imports: [WhatsNewComponent],
      providers: [
        { provide: HttpClient, useValue: { get: () => of(entries) } },
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
