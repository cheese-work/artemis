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
import { HostsService } from '../../services/hosts.service';
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

const httpError = (status: number, body: unknown = {}) =>
  throwError(() => new HttpErrorResponse({ status, error: body }));

describe('RunLibraryComponent', () => {
  let runs: jasmine.SpyObj<RunsService>;
  let hosts: jasmine.SpyObj<HostsService>;
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
        { provide: HostsService, useValue: hosts }
      ]
    });
    harness = await RouterTestingHarness.create();
    router = TestBed.inject(Router);
  });

  describe('rows', () => {
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
      expect(rows[0].querySelector('.run-date')!.textContent).toMatch(/Oct 4, 2026/);
      expect(rows[1].querySelector('.run-outcome')!.textContent).toContain('Interrupted');
      expect(rows[1].querySelector('.run-device')!.textContent).toContain('A browser');
      expect(rows[1].querySelector('.run-recording')!.textContent).toContain('Video unknown');
    });

    it('shows status as an icon plus text, never colour alone', async () => {
      await open('/runs');
      const badge = q('.run-outcome')!;
      expect(badge.querySelector('.material-symbols-outlined')).not.toBeNull();
      expect(badge.textContent!.replace(/\s+/g, ' ')).toContain('Passed');
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
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ cursor: 'next-page' });
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
      expect(runs.list.calls.mostRecent().args[1]).toBeUndefined();

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
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ cursor: 'old-cursor' });

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
      expect(runs.list.calls.mostRecent().args[1]).toEqual({ cursor: 'cursor-1' });
      expect(qa('a.run-row').length).toBe(2);
      expect(q('button.load-more')).toBeNull();
    });
  });

  describe('keyboard walkthrough', () => {
    const FOCUSABLE = 'a[href], button, input, select, summary, [tabindex]:not([tabindex="-1"])';
    const nameOf = (el: Element) =>
      el.getAttribute('aria-label') || (el.textContent ?? '').replace(/\s+/g, ' ').trim();
    const visibleControls = () =>
      qa<HTMLElement>(FOCUSABLE).filter(
        (el) => !(el as HTMLButtonElement).disabled && el.checkVisibility()
      );

    it('tabs through search, filters, then the rows, in reading order, all natively focusable', async () => {
      await open(
        '/runs',
        of(page([run(), run({ session_id: '55555555-5d7e-4a10-9c33-0e1f2a3b4c5d', prompt: 'Second' })], 'c'))
      );
      const names = visibleControls().map(nameOf);
      expect(names.slice(0, 6)).toEqual(['Search runs', 'Search', 'Status', 'From date', 'To date', 'More filters']);
      expect(names[6]).toContain('Log in and open settings');
      expect(names[7]).toContain('Second');
      expect(names[8]).toBe('Load more');
      for (const el of visibleControls()) {
        expect(el.tabIndex).toBeGreaterThanOrEqual(0);
        expect(nameOf(el).length).toBeGreaterThan(0);
      }
    });

    it('reveals the More filters controls in order once opened', async () => {
      await open('/runs');
      const details = q<HTMLDetailsElement>('details.more-filters')!;
      details.open = true;
      harness.fixture.detectChanges();
      const names = visibleControls().map(nameOf);
      expect(names.slice(5, 9)).toEqual(['More filters', 'Computer', 'Phone', 'Requested by']);
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
