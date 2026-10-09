import { Component } from '@angular/core';
import { Location } from '@angular/common';
import { provideLocationMocks } from '@angular/common/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { Observable, Subject, of, throwError } from 'rxjs';
import { HostsResponse } from '../../core/models/host.model';
import { RunPage, RunSummary } from '../../core/models/run.model';
import { Session } from '../../core/models/session.model';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { RunsService } from '../../services/runs.service';
import { SELECTED_DEVICE_SERIAL_KEY } from '../../services/system.service';
import { signal } from '@angular/core';
import { RunLibraryComponent } from './run-library.component';

@Component({ standalone: true, template: 'viewer stub' })
class ViewerStubComponent {}

const ID = '3f2b9c1a-5d7e-4a10-9c33-0e1f2a3b4c5d';
const NOW = Math.floor(Date.now() / 1000);

const run = (over: Partial<RunSummary> = {}): RunSummary => ({
  session_id: ID,
  prompt: 'Log in and open settings',
  status: 'completed',
  interrupt_reason: null,
  start_time: Date.UTC(2026, 9, 4, 12) / 1000,
  end_time: Date.UTC(2026, 9, 4, 12, 5) / 1000,
  host_id: null,
  device_ref: { host_id: null, serial: 'emulator-5554' },
  requested_by: 'qa@example.test',
  pinned: false,
  recordings: [],
  ...over
});

const page = (runs: RunSummary[], next_cursor: string | null = null): RunPage => ({
  runs,
  next_cursor,
  warnings: []
});

const hostsResponse: HostsResponse = {
  enabled: true,
  hosts: [{ id: 'h1', name: 'Desk Mac' } as never],
  devices: []
};

const QA_IDENTITY: AdminIdentity = { email: 'qa@example.test', admin: false, auth_mode: 'cloudflare', reason: null };
const ADMIN_IDENTITY: AdminIdentity = { email: 'admin@example.test', admin: true, auth_mode: 'cloudflare', reason: null };

const httpError = (status: number, body: unknown = {}) =>
  throwError(() => new HttpErrorResponse({ status, error: body }));

describe('RunLibraryComponent', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let hosts: jasmine.SpyObj<HostsService>;
  let adminApi: jasmine.SpyObj<AdminConfigService>;
  let harness: RouterTestingHarness;
  let router: Router;
  let root: HTMLElement;
  let component: RunLibraryComponent;

  const q = <T extends Element>(selector: string) => root.querySelector<T>(selector);
  const qa = <T extends Element>(selector: string) => Array.from(root.querySelectorAll<T>(selector));

  async function open(url: string, list: Observable<RunPage> = of(page([run()]))) {
    runs.list.and.returnValue(list);
    component = await harness.navigateByUrl(url, RunLibraryComponent);
    harness.fixture.detectChanges();
    await harness.fixture.whenStable();
    harness.fixture.detectChanges();
    root = harness.fixture.nativeElement;
  }

  async function settle() {
    harness.fixture.detectChanges();
    await harness.fixture.whenStable();
    harness.fixture.detectChanges();
  }

  function type(selector: string, value: string) {
    const input = q<HTMLInputElement>(selector)!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
  }

  function submitSearch() {
    q<HTMLFormElement>('form[role="search"]')!.dispatchEvent(new Event('submit', { cancelable: true }));
  }

  function choose(selector: string, value: string) {
    const select = q<HTMLSelectElement>(selector)!;
    select.value = value;
    select.dispatchEvent(new Event('change'));
  }

  beforeEach(async () => {
    localStorage.removeItem(SELECTED_DEVICE_SERIAL_KEY);
    runs = jasmine.createSpyObj<RunsService>('RunsService', ['list', 'get'], {
      lastLibraryQuery: signal<Record<string, string>>({})
    });
    hosts = jasmine.createSpyObj<HostsService>('HostsService', ['list']);
    hosts.list.and.returnValue(of(hostsResponse));
    adminApi = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    adminApi.getIdentity.and.returnValue(of(QA_IDENTITY));
    TestBed.configureTestingModule({
      imports: [RunLibraryComponent],
      providers: [
        provideRouter([
          { path: 'runs', component: RunLibraryComponent },
          { path: 'runs/:id', component: ViewerStubComponent },
          { path: 'workspace', component: ViewerStubComponent }
        ]),
        provideLocationMocks(),
        { provide: RunsService, useValue: runs },
        { provide: HostsService, useValue: hosts },
        { provide: AdminConfigService, useValue: adminApi }
      ]
    });
    harness = await RouterTestingHarness.create();
    router = TestBed.inject(Router);
  });

  describe('rows', () => {
    it('renders 56px rows with an execution word, package and mono time, never a Passed chip', async () => {
      await open('/runs', of(page([run({ app_package: 'com.example.shop' })])));
      expect(q('.run-meta')!.textContent!.trim()).toBe('Completed · com.example.shop');
      expect(q('.run-row')!.textContent).not.toContain('Passed');
      expect(q('.run-icon')!.classList).toContain('tone-neutral');
      expect(q('.run-icon')!.classList).not.toContain('tone-ok');
      expect(q('.run-icon .material-symbols-outlined')!.textContent!.trim()).toBe('description');
      expect(q('.run-package')!.getBoundingClientRect().left - q('.run-outcome')!.getBoundingClientRect().right).toBeGreaterThanOrEqual(4);
      expect(getComputedStyle(q('.run-package')!).fontFamily).toContain('JetBrains Mono');
      expect(q('.run-row')!.getBoundingClientRect().height).toBe(56);
    });

    for (const verdictView of [
      { verdict: 'pass', label: 'Pass', icon: 'check_circle', tone: 'ok' },
      { verdict: 'fail', label: 'Fail', icon: 'cancel', tone: 'danger' },
      { verdict: 'inconclusive', label: 'Inconclusive', icon: 'help', tone: 'warn' }
    ] as const) {
      it(`uses the ${verdictView.verdict} verdict only when the catalog supplies one`, async () => {
        await open('/runs', of(page([run({ verdict: verdictView.verdict })])));
        expect(q('.run-outcome')!.textContent).toBe(verdictView.label);
        expect(q('.run-icon')!.classList).toContain(`tone-${verdictView.tone}`);
        expect(q('.run-icon .material-symbols-outlined')!.textContent!.trim()).toBe(verdictView.icon);
      });
    }

    it('groups history by local calendar day, including yesterday, older dates and unknown dates', async () => {
      const today = new Date();
      today.setHours(12, 0, 0, 0);
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      const older = new Date(today);
      older.setDate(older.getDate() - 8);
      await open('/runs', of(page([
        run({ session_id: 'a', start_time: today.getTime() / 1000 }),
        run({ session_id: 'b', start_time: today.getTime() / 1000 - 3600 }),
        run({ session_id: 'c', start_time: yesterday.getTime() / 1000 }),
        run({ session_id: 'd', start_time: older.getTime() / 1000 }),
        run({ session_id: 'e', start_time: null })
      ])));
      const headings = qa('.date-group-title').map((heading) => heading.textContent!.trim());
      expect(headings.slice(0, 2)).toEqual(['Today', 'Yesterday']);
      expect(headings[2]).toBe(older.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }));
      expect(headings[3]).toBe('Date unknown');
      expect(qa('.date-group')[0].querySelectorAll('.run-row').length).toBe(2);
    });

    it('puts running and queued runs in Queue without duplicating history and emits targeted actions', async () => {
      runs.list.and.returnValue(of(page([run({ session_id: 'running', status: 'running' }), run()])));
      const fixture = TestBed.createComponent(RunLibraryComponent);
      const sessions: Session[] = [
        { session_id: 'queued', initial_goal: '**Next** task', status: 'pending', start_time: 2 },
        { session_id: 'running', initial_goal: 'Current task', status: 'running', start_time: 3 }
      ];
      fixture.componentRef.setInput('sessions', sessions);
      const stop = jasmine.createSpy('stopRun');
      fixture.componentInstance.stopRun.subscribe(stop);
      fixture.detectChanges();
      const queue = fixture.nativeElement.querySelector('.queue-section') as HTMLElement;
      expect(Array.from(queue.querySelectorAll('.run-prompt')).map((title) => title.textContent)).toEqual(['Current task', 'Next task']);
      (queue.querySelector('.queue-stop') as HTMLButtonElement).click();
      (queue.querySelector('.queue-cancel') as HTMLButtonElement).click();
      expect(stop.calls.allArgs()).toEqual([['running'], ['queued']]);
      expect(fixture.nativeElement.querySelectorAll('.date-group .run-row').length).toBe(1);
      for (const button of Array.from(queue.querySelectorAll('button'))) {
        const box = button.getBoundingClientRect();
        expect(box.width).toBeGreaterThanOrEqual(44);
        expect(box.height).toBeGreaterThanOrEqual(44);
      }
      fixture.destroy();
    });

    it('hides queue mutations on read-only catalog runs', async () => {
      await open('/runs?scope=everyone', of(page([run({ status: 'running', read_only: true })])));
      expect(q('.queue-section .run-row')).not.toBeNull();
      expect(q('.queue-section button')).toBeNull();
    });

    it('selects a queued run in Workspace without navigating or starting another run', () => {
      runs.list.and.returnValue(of(page([run({ status: 'pending' })])));
      const fixture = TestBed.createComponent(RunLibraryComponent);
      fixture.componentRef.setInput('workspace', true);
      fixture.componentRef.setInput('selectedRunId', ID);
      const select = jasmine.createSpy('selectRun');
      fixture.componentInstance.selectRun.subscribe(select);
      fixture.detectChanges();
      const link = fixture.nativeElement.querySelector('.queue-row a') as HTMLAnchorElement;
      const click = new MouseEvent('click', { bubbles: true, cancelable: true });
      link.dispatchEvent(click);
      expect(click.defaultPrevented).toBeTrue();
      expect(select).toHaveBeenCalledOnceWith(ID);
      expect(link.getAttribute('aria-current')).toBe('page');
      fixture.destroy();
    });

    it('renders removable filters with 44px hit boxes and keeps search when a chip is cleared', async () => {
      await open('/runs?q=login&status=failed');
      const chip = q<HTMLButtonElement>('.filter-chips button')!;
      const box = chip.getBoundingClientRect();
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      chip.click();
      await settle();
      expect(router.url).toBe('/runs?q=login');
      expect(q('.filter-chips')).toBeNull();
    });

    it('focuses search with / but leaves editable controls and modified shortcuts alone', async () => {
      await open('/runs');
      const search = q<HTMLInputElement>('input[type="search"]')!;
      const shortcut = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true });
      document.dispatchEvent(shortcut);
      expect(document.activeElement).toBe(search);
      expect(shortcut.defaultPrevented).toBeTrue();
      const typed = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true });
      search.dispatchEvent(typed);
      expect(typed.defaultPrevented).toBeFalse();
      q<HTMLButtonElement>('[role="tab"]')!.focus();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: '/', ctrlKey: true }));
      expect(document.activeElement).not.toBe(search);
    });

    it('gives tabs, search and New run their own 44px hit boxes at column and mobile widths', async () => {
      await open('/runs');
      for (const width of [360, 390]) {
        root.style.width = `${width}px`;
        for (const control of qa<HTMLElement>('[role="tab"], input[type="search"], .new-run')) {
          const box = control.getBoundingClientRect();
          expect(box.width).withContext(`${width}px width`).toBeGreaterThanOrEqual(44);
          expect(box.height).withContext(`${width}px height`).toBeGreaterThanOrEqual(44);
        }
      }
    });

    it('shows the first prompt line without markdown in each run row', async () => {
      runs.list.and.returnValue(of(page([run({ prompt: '\n## **Open** _Settings_\nCheck every toggle.' })])));
      const fixture = TestBed.createComponent(RunLibraryComponent);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();
      const title = fixture.nativeElement.querySelector('.run-prompt') as HTMLElement;
      expect(title.textContent).toBe('Open Settings');
      expect(title.title).toBe('Open Settings');
      expect(getComputedStyle(title).webkitLineClamp).toBe('2');
    });

    it('compact history scrolls inside the region whose position is saved and restored', async () => {
      runs.list.and.returnValue(of(page(Array.from({ length: 20 }, (_, index) => run({ session_id: `run-${index}` })))));
      const fixture = TestBed.createComponent(RunLibraryComponent);
      fixture.componentRef.setInput('compact', true);
      fixture.detectChanges();
      await fixture.whenStable();
      const region = fixture.nativeElement.querySelector('.library-scroll') as HTMLElement;
      expect(getComputedStyle(region).overflowY).toBe('auto');
      expect(region.scrollHeight).toBeGreaterThan(region.clientHeight);
      region.scrollTop = 150;
      fixture.componentInstance.rememberPosition();
      expect(runs.lastLibraryQuery()).toEqual({ scroll: '150' });
      fixture.destroy();

      await router.navigateByUrl('/runs?scroll=150');
      const restored = TestBed.createComponent(RunLibraryComponent);
      restored.componentRef.setInput('compact', true);
      restored.detectChanges();
      await restored.whenStable();
      expect(restored.nativeElement.querySelector('.library-scroll').scrollTop).toBe(150);
      restored.destroy();
    });

    it('refreshes the current scope and filters without navigating or reusing a page cursor', async () => {
      await open('/runs?scope=everyone&status=completed&q=login', of(page([run()], 'old-cursor')));
      const fixture = TestBed.createComponent(RunLibraryComponent);
      fixture.componentRef.setInput('refreshKey', 'initial');
      fixture.detectChanges();
      const before = runs.list.calls.count();
      const url = router.url;
      runs.list.and.returnValue(of(page([run({ session_id: 'new-run' })])));
      fixture.componentRef.setInput('refreshKey', 'completed');
      fixture.detectChanges();
      expect(runs.list.calls.count()).toBe(before + 1);
      expect(runs.list.calls.mostRecent().args[0]).toEqual(jasmine.objectContaining({ status: 'completed', q: 'login' }));
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'everyone' });
      expect(fixture.componentInstance.nextCursor()).toBeNull();
      expect(router.url).toBe(url);
      fixture.destroy();
    });

    it('restores Everyone in the URL with filters and read-only owner-labelled links', async () => {
      await open('/runs?scope=everyone&q=login&status=failed&from=2026-10-01');
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'everyone' });
      expect(q('.run-owner')?.textContent).toContain('qa@example.test');
      expect(q('.run-read-only')).toBeNull();
      expect(q('.run-row')!.textContent).not.toContain('Read-only');
      expect(q<HTMLAnchorElement>('.run-row')?.getAttribute('href')).toContain('review=1');
      expect(root.textContent).toContain('Videos and screenshots are not redacted');
      expect(qa<HTMLButtonElement>('[role="tab"]').map((tab) => tab.getAttribute('aria-selected')))
        .toEqual(['false', 'true']);
    });

    it('switches tabs by keyboard without losing search, status or date filters', async () => {
      await open('/runs?q=login&status=failed&from=2026-10-01&to=2026-10-07&scroll=100');
      const mine = q<HTMLButtonElement>('[role="tab"]')!;
      mine.focus();
      mine.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
      await settle();
      expect(router.url).toContain('scope=everyone');
      expect(router.url).toContain('q=login');
      expect(router.url).toContain('status=failed');
      expect(router.url).toContain('from=2026-10-01');
      expect(router.url).toContain('to=2026-10-07');
      expect(router.url).not.toContain('scroll=');
      expect(document.activeElement).toBe(qa<HTMLButtonElement>('[role="tab"]')[1]);
      component.clearFilters();
      await settle();
      expect(router.url).toBe('/runs?scope=everyone');
      qa<HTMLButtonElement>('[role="tab"]')[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
      await settle();
      expect(router.url).toBe('/runs');
    });

    it('explains an empty team tab separately from an empty My runs tab', async () => {
      await open('/runs?scope=everyone', of(page([])));
      expect(root.textContent).toContain('No shared runs yet.');
      expect(root.textContent).not.toContain('Start one in Workspace');
    });

    it('drops stale prompts and cursors before loading a different owner tab', async () => {
      await open('/runs', of(page([run({ prompt: 'Private prompt' })], 'mine-cursor')));
      const team = new Subject<RunPage>();
      runs.list.and.returnValue(team);
      qa<HTMLButtonElement>('[role="tab"]')[1].click();
      await settle();
      expect(root.textContent).not.toContain('Private prompt');
      expect(component.nextCursor()).toBeNull();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'everyone' });
      team.next(page([run({ prompt: 'Redacted team prompt' })], 'team-cursor'));
      await settle();
      runs.list.and.returnValue(of(page([])));
      component.loadMore();
      await settle();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'everyone', cursor: 'team-cursor' });
    });

    it('characterizes status and date URL filters without changing the selected device', async () => {
      localStorage.setItem(SELECTED_DEVICE_SERIAL_KEY, 'my-phone');
      await open('/runs?status=failed&from=2026-10-01&to=2026-10-07');
      expect(component.filters().status).toBe('failed');
      expect(component.filters().from).toBe('2026-10-01');
      expect(component.filters().to).toBe('2026-10-07');
      expect(q<HTMLAnchorElement>('.run-row')?.getAttribute('href')).toBe(`/runs/${ID}`);
      expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe('my-phone');
    });

    it('lists runs newest first as links with prompt, outcome, recording, device and computer, and date', async () => {
      await open(
        '/runs',
        of(
          page([
            run({
              session_id: ID,
              status: 'failed',
              host_id: 'h1',
              recordings: [{ recording_id: 'r1', capture: 'stopped', transfer: 'uploaded' }]
            }),
            run({ session_id: '11111111-5d7e-4a10-9c33-0e1f2a3b4c5d', prompt: 'Older run', status: 'interrupted' })
          ])
        )
      );
      const rows = qa<HTMLAnchorElement>('ol.run-list > li > a.run-row');
      expect(rows.length).toBe(2);
      expect(rows[0].getAttribute('href')).toBe(`/runs/${ID}`);
      expect(rows[0].querySelector('.run-prompt')!.textContent).toContain('Log in and open settings');
      expect(rows[0].querySelector('.run-outcome')!.textContent).toContain('Failed');
      expect(rows[0].querySelector('.run-recording')!.textContent).toContain('Video uploaded');
      expect(rows[0].querySelector('.run-device')!.textContent).toContain('emulator-5554');
      expect(rows[0].querySelector('.run-device')!.textContent).toContain('Desk Mac');
      expect(rows[0].querySelector('.run-date')!.getAttribute('title')).toMatch(/Oct 4, 2026/);
      expect(rows[1].querySelector('.run-outcome')!.textContent).toContain('Interrupted');
      expect(rows[1].querySelector('.run-device')!.textContent!.trim()).toBe('emulator-5554');
      expect(rows[1].querySelector('.run-recording')).toBeNull();
    });

    it('hides unknown video badges but preserves pending, failed and missing video states', async () => {
      await open('/runs', of(page([
        run({ session_id: 'unknown', recordings: [{ recording_id: 'r0', capture: null, transfer: null }] }),
        run({ session_id: 'pending', recordings: [{ recording_id: 'r1', capture: 'pending', transfer: null }] }),
        run({ session_id: 'failed', recordings: [{ recording_id: 'r2', capture: 'stopped', transfer: 'failed' }] }),
        run({ session_id: 'missing', recordings: [{ recording_id: 'r3', capture: 'missing:disabled', transfer: null }] })
      ])));
      const rows = qa<HTMLElement>('.run-row');
      expect(rows[0].querySelector('.run-recording')).toBeNull();
      expect(rows.slice(1).map((row) => row.querySelector('.run-recording')!.textContent!.trim()))
        .toEqual(['Video pending', 'Upload failed', 'No video']);
    });

    it('uses tabular mono for serials and dates without changing the row hit area', async () => {
      await open('/runs');
      for (const selector of ['.run-serial', '.run-date']) {
        const style = getComputedStyle(q(selector)!);
        expect(style.fontFamily).toContain('JetBrains Mono');
        expect(style.fontVariantNumeric).toBe('tabular-nums');
      }
      const box = q('.run-row')!.getBoundingClientRect();
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    });

    it('keeps a browser or network address out of the device text, and in the tooltip (R3)', async () => {
      const serials = ['127.0.0.1:39129', 'pixel:5555', '192.168.1.12:5555'];
      await open(
        '/runs',
        of(page(serials.map((serial, i) => run({ session_id: `${i}1111111-5d7e-4a10-9c33-0e1f2a3b4c5d`, device_ref: { host_id: null, serial } }))))
      );
      const devices = qa<HTMLElement>('.run-device');
      expect(devices.length).toBe(3);
      devices.forEach((el, i) => {
        expect(el.textContent).not.toContain(serials[i]);
        expect(el.textContent).not.toContain('Unknown');
        expect(el.getAttribute('title')).toContain(serials[i]);
      });
      expect(devices[0].textContent).toContain('Phone via a browser');
      expect(devices[1].textContent).toContain('Wireless phone');
    });

    it('shows status as an icon plus text, never colour alone', async () => {
      await open('/runs');
      expect(q('.run-icon .material-symbols-outlined')).not.toBeNull();
      expect(q('.run-outcome')!.textContent).toContain('Completed');
    });

    it('shows "Expires on" only when 7 days or fewer remain', async () => {
      await open(
        '/runs',
        of(
          page([
            run({ expires_at: NOW + 3 * 86400 }),
            run({ session_id: '22222222-5d7e-4a10-9c33-0e1f2a3b4c5d', expires_at: NOW + 20 * 86400 }),
            run({ session_id: '33333333-5d7e-4a10-9c33-0e1f2a3b4c5d' })
          ])
        )
      );
      const expires = qa('.run-expires');
      expect(expires.length).toBe(1);
      expect(expires[0].textContent).toMatch(/^\s*Expires on /);
    });

    it('requests the first page with no filters and never touches the selected device', async () => {
      localStorage.setItem(SELECTED_DEVICE_SERIAL_KEY, 'R58M123');
      await open('/runs');
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(runs.list.calls.mostRecent().args[0]).toEqual({
        q: '', status: '', from: '', to: '', device: '', host: '', requester: ''
      });
      expect(localStorage.getItem(SELECTED_DEVICE_SERIAL_KEY)).toBe('R58M123');
    });
  });

  describe('search modes', () => {
    it('searches text through the URL and sends it verbatim for the server to quote', async () => {
      await open('/runs');
      runs.list.calls.reset();
      type('input[type="search"]', 'a OR "b');
      submitSearch();
      await settle();
      expect(router.url).toBe('/runs?q=a%20OR%20%22b');
      expect(runs.list.calls.mostRecent().args[0].q).toBe('a OR "b');
      expect(runs.get).not.toHaveBeenCalled();
    });

    it('jumps to the run when the text is an 8-character id', async () => {
      runs.get.and.returnValue(of(run()));
      await open('/runs');
      type('input[type="search"]', '3F2B9C1A');
      submitSearch();
      await settle();
      expect(runs.get).toHaveBeenCalledWith('3f2b9c1a');
      expect(router.url).toBe(`/runs/${ID}`);
    });

    it('jumps on a full uuid', async () => {
      runs.get.and.returnValue(of(run()));
      await open('/runs');
      type('input[type="search"]', ID);
      submitSearch();
      await settle();
      expect(runs.get).toHaveBeenCalledWith(ID);
      expect(router.url).toBe(`/runs/${ID}`);
    });

    it('falls back to text search when an 8-character word is not a run id', async () => {
      runs.get.and.returnValue(httpError(404, { error: 'not_found' }));
      await open('/runs');
      runs.list.calls.reset();
      type('input[type="search"]', 'deadbeef');
      submitSearch();
      await settle();
      expect(router.url).toBe('/runs?q=deadbeef');
      expect(runs.list.calls.mostRecent().args[0].q).toBe('deadbeef');
    });

    for (const status of [410, 409]) {
      it(`opens the viewer for an id the server answers with ${status}, so it can explain`, async () => {
        runs.get.and.returnValue(httpError(status, { error: 'x' }));
        await open('/runs');
        type('input[type="search"]', '3f2b9c1a');
        submitSearch();
        await settle();
        expect(router.url).toBe('/runs/3f2b9c1a');
      });
    }

    it('clearing the box and searching again lists everything', async () => {
      await open('/runs?q=login');
      expect(q<HTMLInputElement>('input[type="search"]')!.value).toBe('login');
      type('input[type="search"]', '');
      submitSearch();
      await settle();
      expect(router.url).toBe('/runs');
    });
  });

  describe('filter persistence', () => {
    it('restores every control and the request from the URL', async () => {
      await open(
        '/runs?q=login&status=failed&from=2026-10-01&to=2026-10-03&device=emulator-5554&host=local&requester=qa%40example.test'
      );
      expect(q<HTMLInputElement>('input[type="search"]')!.value).toBe('login');
      expect(q<HTMLSelectElement>('select[aria-label="Status"]')!.value).toBe('failed');
      expect(q<HTMLInputElement>('input[aria-label="From date"]')!.value).toBe('2026-10-01');
      expect(q<HTMLInputElement>('input[aria-label="To date"]')!.value).toBe('2026-10-03');
      expect(q<HTMLDetailsElement>('details.more-filters')!.open).toBe(true);
      expect(q<HTMLInputElement>('input[aria-label="Phone"]')!.value).toBe('emulator-5554');
      expect(q<HTMLSelectElement>('select[aria-label="Computer"]')!.value).toBe('local');
      expect(q<HTMLInputElement>('input[aria-label="Requested by"]')!.value).toBe('qa@example.test');
      expect(runs.list.calls.mostRecent().args[0]).toEqual({
        q: 'login',
        status: 'failed',
        from: '2026-10-01',
        to: '2026-10-03',
        device: 'emulator-5554',
        host: 'local',
        requester: 'qa@example.test'
      });
    });

    it('keeps "More filters" closed until one of its filters is set', async () => {
      await open('/runs?status=failed');
      expect(q<HTMLDetailsElement>('details.more-filters')!.open).toBe(false);
    });

    it('writes a changed filter to the URL without adding a history entry, and reloads', async () => {
      await open('/runs');
      const location = TestBed.inject(Location);
      const before = (location as unknown as { historyLength?: number }).historyLength;
      runs.list.calls.reset();
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      expect(router.url).toBe('/runs?status=failed');
      expect(runs.list.calls.mostRecent().args[0].status).toBe('failed');
      expect(runs.list).toHaveBeenCalledTimes(1);
      if (before !== undefined) {
        expect((location as unknown as { historyLength: number }).historyLength).toBe(before);
      }
    });

    it('survives opening a run and coming back through the remembered query', async () => {
      await open('/runs?status=failed&q=login');
      await harness.navigateByUrl(`/runs/${ID}`);
      runs.list.calls.reset();
      await router.navigate(['/runs'], { queryParams: runs.lastLibraryQuery() });
      await settle();
      expect(router.url).toBe('/runs?q=login&status=failed');
      expect(runs.list.calls.mostRecent().args[0].status).toBe('failed');
      expect(runs.list.calls.mostRecent().args[0].q).toBe('login');
    });

    it('remembers the library query for the viewer\'s "Back to runs" link', async () => {
      await open('/runs?status=failed&scroll=300');
      expect(runs.lastLibraryQuery()).toEqual({ status: 'failed', scroll: '300' });
    });

    it('"Clear filters" resets the URL to /runs', async () => {
      await open('/runs?status=failed', of(page([])));
      q<HTMLButtonElement>('.state-no-match button')!.click();
      await settle();
      expect(router.url).toBe('/runs');
    });

    it('P2: the busy list keeps full text contrast: nothing on the rows is dimmed, and it says so in words', async () => {
      await open('/runs', of(page([run(), run({ status: 'failed', session_id: '66666666-5d7e-4a10-9c33-0e1f2a3b4c5d' })])));
      runs.list.and.returnValue(new Subject<RunPage>());
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      expect(q('ol.run-list')!.getAttribute('aria-busy')).toBe('true');
      const effectiveOpacity = (el: Element) => {
        let value = 1;
        for (let node: Element | null = el; node; node = node.parentElement) value *= Number(getComputedStyle(node).opacity);
        return value;
      };
      for (const el of qa('.run-prompt, .run-outcome, .run-meta, .run-meta span')) {
        expect(effectiveOpacity(el)).toBe(1);
      }
      expect(q('.updating')!.textContent).toContain('Updating results');
    });

    it('keeps the previous rows on screen, marked busy, while a new filter loads', async () => {
      await open('/runs');
      const next = new Subject<RunPage>();
      runs.list.and.returnValue(next);
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      expect(qa('a.run-row').length).toBe(1);
      expect(q('ol.run-list')!.getAttribute('aria-busy')).toBe('true');
      next.next(page([run({ status: 'failed' })]));
      next.complete();
      await settle();
      expect(q('ol.run-list')!.getAttribute('aria-busy')).toBe('false');
    });
  });

  describe('scroll position in the URL', () => {
    const many = (n: number, next: string | null = null) =>
      page(
        Array.from({ length: n }, (_, i) =>
          run({ session_id: `${String(i).padStart(8, '0')}-5d7e-4a10-9c33-0e1f2a3b4c5d`, prompt: `Run ${i}` })
        ),
        next
      );
    const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

    async function openPending(url: string) {
      const first = new Subject<RunPage>();
      runs.list.and.returnValue(first);
      await harness.navigateByUrl(url, RunLibraryComponent);
      harness.fixture.autoDetectChanges(); // as in the app, rows are laid out before the restore timer fires
      root = harness.fixture.nativeElement;
      q<HTMLElement>('.library-scroll')!.style.height = '200px';
      return first;
    }

    it('P2: opening a row inside the scroll debounce still saves the position for the way back', async () => {
      await open('/runs', of(many(12)));
      const replace = spyOn(TestBed.inject(Location), 'replaceState').and.callThrough();
      const region = q<HTMLElement>('.library-scroll')!;
      region.style.height = '200px';
      region.scrollTop = 120;
      region.dispatchEvent(new Event('scroll'));
      q<HTMLAnchorElement>('a.run-row')!.click(); // well inside the 250 ms debounce
      await settle();
      expect(replace).toHaveBeenCalled();
      expect(replace.calls.mostRecent().args.join('')).toContain('scroll=120');
      expect(runs.lastLibraryQuery()).toEqual({ scroll: '120' });
      expect(router.url).toContain('/runs/');
    });

    it('writes the scroll position to the URL, and a scroll alone does not reload the list', async () => {
      await open('/runs', of(many(12)));
      const region = q<HTMLElement>('.library-scroll')!;
      region.style.height = '200px';
      region.scrollTop = 120;
      region.dispatchEvent(new Event('scroll'));
      await wait(350);
      await settle();
      expect(router.url).toBe('/runs?scroll=120');
      expect(runs.list).toHaveBeenCalledTimes(1);
    });

    it('restores the saved position once the first page is on screen', async () => {
      const first = await openPending('/runs?scroll=150');
      first.next(many(12));
      first.complete();
      await settle();
      await wait(50);
      expect(q<HTMLElement>('.library-scroll')!.scrollTop).toBe(150);
    });

    it('loads more pages until the saved position exists, then scrolls to it', async () => {
      const first = await openPending('/runs?scroll=700');
      runs.list.and.returnValue(of(many(12)));
      first.next(many(3, 'next-page'));
      first.complete();
      await settle();
      await wait(50);
      await settle();
      await wait(50);
      expect(runs.list).toHaveBeenCalledTimes(2);
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine', cursor: 'next-page' });
      expect(q<HTMLElement>('.library-scroll')!.scrollTop).toBe(700);
    });
  });

  describe('paging while the query changes', () => {
    const failedRow = (n: number) =>
      run({ session_id: `${String(n).padStart(8, '0')}-5d7e-4a10-9c33-0e1f2a3b4c5d`, status: 'failed', prompt: `Failed ${n}` });

    it('P1: Load more is gone and inert while a new filter is loading, and no old cursor is ever sent', async () => {
      await open('/runs', of(page([run({ status: 'completed' })], 'old-cursor')));
      expect(q('button.load-more')).not.toBeNull();
      const reload = new Subject<RunPage>();
      runs.list.calls.reset();
      runs.list.and.returnValue(reload);
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(q('button.load-more')).toBeNull(); // the old cursor belongs to the old query

      component.loadMore(); // a queued or programmatic call must not consume it either
      await settle();
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine' });

      reload.next(page([failedRow(1)]));
      reload.complete();
      await settle();
      expect(qa('a.run-row').map((a) => a.textContent)).toEqual([jasmine.stringContaining('Failed 1')]);
      expect(runs.list.calls.allArgs().some(([, options]) => options?.cursor === 'old-cursor')).toBe(false);
    });

    it('P1: a Load more page that is still in flight when the filter changes never lands in the new list', async () => {
      await open('/runs', of(page([run({ status: 'completed' })], 'old-cursor')));
      const older = new Subject<RunPage>();
      runs.list.and.returnValue(older);
      q<HTMLButtonElement>('button.load-more')!.click();
      await settle();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine', cursor: 'old-cursor' });

      runs.list.and.returnValue(of(page([failedRow(2)])));
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      older.next(page([run({ status: 'completed', prompt: 'Stale completed row' })]));
      await settle();
      expect(qa('a.run-row').length).toBe(1);
      expect(root.textContent).not.toContain('Stale completed row');
      expect(q('ol.run-list')!.getAttribute('aria-busy')).toBe('false');
      expect(q<HTMLButtonElement>('button.load-more')).toBeNull();
    });

    it('P2: changing a filter drops the initial scroll restore target', async () => {
      const first = new Subject<RunPage>();
      runs.list.and.returnValue(first);
      component = await harness.navigateByUrl('/runs?scroll=400', RunLibraryComponent);
      harness.fixture.autoDetectChanges();
      root = harness.fixture.nativeElement;
      q<HTMLElement>('.library-scroll')!.style.height = '200px';
      runs.list.and.returnValue(of(page(Array.from({ length: 12 }, (_, i) => failedRow(i + 10)))));
      choose('select[aria-label="Status"]', 'failed');
      await settle();
      await new Promise((resolve) => setTimeout(resolve, 60));
      expect(q<HTMLElement>('.library-scroll')!.scrollTop).toBe(0);
    });
  });

  describe('empty versus no match', () => {
    it('says "No runs yet" and points at Workspace when nothing was ever recorded', async () => {
      await open('/runs', of(page([])));
      expect(q('.state-empty')!.textContent).toContain('No runs yet');
      expect(q<HTMLAnchorElement>('.state-empty a')!.getAttribute('href')).toBe('/workspace');
      expect(q('.state-no-match')).toBeNull();
    });

    it('says "No matching runs" with Clear filters when a search or filter hides everything', async () => {
      await open('/runs?q=nothing', of(page([])));
      expect(q('.state-no-match')!.textContent).toContain('No matching runs');
      expect(q('.state-no-match button')!.textContent).toContain('Clear filters');
      expect(q('.state-empty')).toBeNull();
    });
  });

  describe('errors and paging', () => {
    it('shows "Couldn\'t load runs" with Retry, and Retry keeps the filters', async () => {
      await open('/runs?status=failed', httpError(500));
      expect(q('.state-error')!.textContent).toContain("Couldn't load runs");
      runs.list.calls.reset();
      runs.list.and.returnValue(of(page([run({ status: 'failed' })])));
      q<HTMLButtonElement>('.state-error button')!.click();
      await settle();
      expect(runs.list.calls.mostRecent().args[0].status).toBe('failed');
      expect(q('.state-error')).toBeNull();
      expect(qa('a.run-row').length).toBe(1);
    });

    it('explains a catalog that is still getting ready', async () => {
      await open('/runs', httpError(503, { error: 'catalog_not_ready' }));
      expect(q('.state-error')!.textContent).toContain('getting ready');
    });

    it('loads the next page with the cursor and hides Load more on the last page', async () => {
      await open('/runs', of(page([run()], 'cursor-1')));
      expect(q('button.load-more')).not.toBeNull();
      runs.list.and.returnValue(of(page([run({ session_id: '44444444-5d7e-4a10-9c33-0e1f2a3b4c5d' })])));
      q<HTMLButtonElement>('button.load-more')!.click();
      await settle();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine', cursor: 'cursor-1' });
      expect(qa('a.run-row').length).toBe(2);
      expect(q('button.load-more')).toBeNull();
    });
  });

  describe('per-QA scope (CHE-1152)', () => {
    const teamTab = () => q<HTMLButtonElement>('[data-scope="everyone"]')!;
    const myTab = () => q<HTMLButtonElement>('[data-scope="mine"]')!;
    const owners = () => qa('.run-owner').map((el) => el.textContent!.trim());

    it('shows a QA their own runs with no switch and no owner labels', async () => {
      await open('/runs', of(page([run()])));
      expect(q('[role="switch"]')).toBeNull();
      expect(myTab().getAttribute('aria-selected')).toBe('true');
      expect(owners()).toEqual([]);
    });

    it('gives an admin owner tabs with My runs selected and only their own runs asked for', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs', of(page([run({ requested_by: 'admin@example.test' })])));
      expect(teamTab().getAttribute('aria-selected')).toBe('false');
      expect(q('[role="switch"]')).toBeNull();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine' });
      expect(owners()).toEqual([]);
    });

    it('reloads the list for everyone and labels each row with its owner when an admin selects the team tab', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs', of(page([run({ requested_by: 'admin@example.test' })])));
      runs.list.calls.reset();
      runs.list.and.returnValue(
        of(
          page([
            run({ requested_by: 'qa1@example.test' }),
            run({ session_id: '22222222-5d7e-4a10-9c33-0e1f2a3b4c5d', requested_by: null })
          ])
        )
      );
      teamTab().click();
      await settle();
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'everyone' });
      expect(TestBed.inject(OwnerScopeService).showAll()).toBeFalse();
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(owners()).toEqual(['Owner: qa1@example.test', 'Owner: No owner']);
    });

    it('drops the owner labels and reloads again when My runs is selected', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs', of(page([run({ requested_by: 'qa1@example.test' })])));
      teamTab().click();
      await settle();
      runs.list.calls.reset();
      myTab().click();
      await settle();
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(owners()).toEqual([]);
    });

    it('keeps the search text and filters when the scope changes', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs?q=login&status=failed', of(page([])));
      teamTab().click();
      await settle();
      expect(runs.list.calls.mostRecent().args[0]).toEqual(jasmine.objectContaining({ q: 'login', status: 'failed' }));
    });

    it('says "No runs yet" for a QA with nothing, and "No runs match" only when a filter hides everything', async () => {
      await open('/runs', of(page([])));
      expect(q('.state-empty')!.textContent).toContain('No runs yet');
      await router.navigateByUrl('/runs?status=failed');
      await settle();
      expect(q('.state-no-match')!.textContent).toContain('No matching runs');
    });

    it('names the scope in the empty state while an admin looks at all users', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs', of(page([])));
      expect(q('.state-empty')!.textContent).toContain('No runs yet');
      runs.list.and.returnValue(of(page([])));
      teamTab().click();
      await settle();
      expect(q('.state-empty')!.textContent).toContain('No shared runs yet');
    });

    it('loads My runs once without inheriting the admin queue All users scope', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      const scope = TestBed.inject(OwnerScopeService);
      scope.identity.set(ADMIN_IDENTITY);
      scope.setAllUsers(true);
      await open('/runs', of(page([run({ requested_by: 'qa1@example.test' })])));
      expect(runs.list).toHaveBeenCalledTimes(1);
      expect(owners()).toEqual([]);
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ scope: 'mine' });
    });

    it('puts the selected owner tab first in the tab order for an admin, natively focusable', async () => {
      adminApi.getIdentity.and.returnValue(of(ADMIN_IDENTITY));
      await open('/runs', of(page([run()])));
      const focusable = qa<HTMLElement>('a[href], button, input, select, summary').filter(
        (el) => el.tabIndex >= 0 && !(el as HTMLButtonElement).disabled && el.checkVisibility()
      );
      expect(focusable[0].textContent!.trim()).toBe('New run');
      expect(focusable[1]).toBe(myTab());
      expect(focusable[2].getAttribute('aria-label')).toBe('Search runs');
    });
  });

  describe('keyboard walkthrough', () => {
    const FOCUSABLE = 'a[href], button, input, select, summary, [tabindex]:not([tabindex="-1"])';
    const nameOf = (el: Element) =>
      el.getAttribute('aria-label') || (el.textContent ?? '').replace(/\s+/g, ' ').trim();
    const visibleControls = () =>
      qa<HTMLElement>(FOCUSABLE).filter(
        (el) => el.tabIndex >= 0 && !(el as HTMLButtonElement).disabled && el.checkVisibility()
      );

    it('tabs through search, filters, then the rows, in reading order, all natively focusable', async () => {
      await open(
        '/runs',
        of(page([run(), run({ session_id: '55555555-5d7e-4a10-9c33-0e1f2a3b4c5d', prompt: 'Second' })], 'c'))
      );
      const names = visibleControls().map(nameOf);
      expect(names.slice(0, 5)).toEqual(['New run', 'Mine', 'Search runs', 'Search runs', 'Filters']);
      expect(names[5]).toContain('Log in and open settings');
      expect(names[6]).toContain('Second');
      expect(names[7]).toBe('Load more');
      for (const el of visibleControls()) {
        expect(el.tabIndex).toBeGreaterThanOrEqual(0);
        expect(nameOf(el).length).toBeGreaterThan(0);
      }
    });

    it('reveals the More filters controls in order once opened', async () => {
      await open('/runs');
      q<HTMLDetailsElement>('details.filter-options')!.open = true;
      const details = q<HTMLDetailsElement>('details.more-filters')!;
      details.open = true;
      harness.fixture.detectChanges();
      const names = visibleControls().map(nameOf);
      expect(names.slice(8, 12)).toEqual(['More filters', 'Computer', 'Phone', 'Requested by']);
    });

    it('keeps every target at least 24 px and primary targets at least 44 px', async () => {
      await open('/runs', of(page([run()], 'c')));
      for (const el of visibleControls()) {
        const box = el.getBoundingClientRect();
        expect(box.width).toBeGreaterThanOrEqual(24, nameOf(el));
        expect(box.height).toBeGreaterThanOrEqual(24, nameOf(el));
      }
      for (const selector of ['button.search-button', 'a.run-row', 'button.load-more']) {
        expect(q(selector)!.getBoundingClientRect().height).toBeGreaterThanOrEqual(44, selector);
      }
    });

    it('submits the search with Enter inside the search box (a real form)', async () => {
      await open('/runs');
      const form = q<HTMLFormElement>('form[role="search"]')!;
      expect(form.querySelector('input[type="search"]')).not.toBeNull();
      expect(form.querySelector('button[type="submit"]')).not.toBeNull();
    });
  });
});
