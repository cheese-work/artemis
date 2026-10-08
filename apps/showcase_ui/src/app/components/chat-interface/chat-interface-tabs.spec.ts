import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { EMPTY, NEVER, of } from 'rxjs';
import { AgentService } from '../../services/agent.service';
import { HostsService } from '../../services/hosts.service';
import { RunsService } from '../../services/runs.service';
import { SystemService } from '../../services/system.service';
import { WEBUSB_DEVICE_MANAGER } from '../../services/usb-device-relay.service';
import { ChatInterfaceComponent } from './chat-interface.component';

// CHE-1278: the Task Queue / Notes tabs are real tabs: named, one tab stop, arrow keys, a labelled panel.
describe('ChatInterface tabs', () => {
  let root: HTMLElement;
  let activeTab: ReturnType<typeof signal<'tasks' | 'notes'>>;
  const tabs = () => Array.from(root.querySelectorAll<HTMLButtonElement>('.tab-selector-bar [role=tab]'));

  beforeEach(async () => {
    activeTab = signal<'tasks' | 'notes'>('tasks');
    await TestBed.configureTestingModule({
      imports: [ChatInterfaceComponent],
      providers: [
        provideHttpClient(), provideHttpClientTesting(), provideRouter([]),
        { provide: AgentService, useValue: {
          sessions: signal([]), activeTab, agentStatus: signal('idle'), runningSessionId: signal(null), currentSessionId: signal(null),
          currentNotes: signal([]), selectedNoteKey: signal(null), fetchStatus: () => {}
        } },
        { provide: RunsService, useValue: { lastLibraryQuery: signal({}), list: () => NEVER } },
        { provide: HostsService, useValue: { list: () => of({ enabled: true, hosts: [], devices: [] }) } },
        { provide: WEBUSB_DEVICE_MANAGER, useValue: undefined }
      ]
    }).compileComponents();
    spyOn(TestBed.inject(SystemService), 'fetchReadiness').and.returnValue(EMPTY);
    const fixture = TestBed.createComponent(ChatInterfaceComponent);
    document.body.appendChild(fixture.nativeElement);
    fixture.detectChanges();
    root = fixture.nativeElement;
  });

  afterEach(() => root.remove());

  it('names the tablist and marks exactly one tab selected with one tab stop', () => {
    expect(root.querySelector('.tab-selector-bar[role=tablist]')?.getAttribute('aria-label')).toBe('Task queue and notes');
    expect(tabs().map((tab) => tab.querySelector('.tab-text')!.textContent!.trim())).toEqual(['Task Queue', 'Notes & Plans']);
    expect(tabs().map((tab) => tab.getAttribute('aria-selected'))).toEqual(['true', 'false']);
    expect(tabs().map((tab) => tab.tabIndex)).toEqual([0, -1]);
  });

  it('labels the panel with the selected tab', () => {
    const panel = root.querySelector('#queue-panel[role=tabpanel]')!;
    expect(panel.getAttribute('aria-labelledby')).toBe('queue-tab-tasks');
    expect(root.querySelector('#queue-tab-tasks')).toBe(tabs()[0]);
  });

  it('moves between tabs with the arrow keys and keeps focus on the new tab', () => {
    tabs()[0].focus();
    tabs()[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
    expect(activeTab()).toBe('notes');
    expect(document.activeElement).toBe(tabs()[1]);
    tabs()[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true, cancelable: true }));
    expect(activeTab()).toBe('tasks');
  });
});
