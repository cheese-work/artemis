import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  OnChanges,
  SimpleChanges,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { Location } from '@angular/common';
import { ActivatedRoute, Router, RouterLink, convertToParamMap } from '@angular/router';
import { Subscription, map } from 'rxjs';
import { Computer, RegistryDevice } from '../../core/models/host.model';
import { RunSummary } from '../../core/models/run.model';
import { GoalImage, Session } from '../../core/models/session.model';
import { HostsService } from '../../services/hosts.service';
import { RunsService } from '../../services/runs.service';
import { RunCardComponent } from './run-card.component';
import { LabelableDevice } from '../../utils/device-label.util';
import {
  EMPTY_FILTERS,
  RunFilters,
  STATUS_FILTERS,
  filtersFromQuery,
  filtersToQuery,
  hasActiveFilters,
  scrollFromQuery
} from '../../utils/run-filters.util';
import {
  RUN_STRINGS,
  MEDIA_NOTICE
} from '../../utils/run-library-strings';
import { runStatusView, sessionStatusView } from '../../utils/run-status.util';
import { classifySearch } from '../../utils/run-search.util';
import { runTitle } from '../../utils/run-title.util';

@Component({
  selector: 'app-run-library',
  standalone: true,
  imports: [RouterLink, RunCardComponent],
  templateUrl: './run-library.component.html',
  styleUrl: './run-library.component.scss',
  host: { '[class.compact]': 'compact()', '(document:keydown)': 'focusSearch($event)' },
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunLibraryComponent implements OnChanges {
  private readonly runsApi = inject(RunsService);
  private readonly hostsApi = inject(HostsService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly location = inject(Location);
  private readonly destroyRef = inject(DestroyRef);

  public readonly strings = RUN_STRINGS;
  public readonly statusOptions = STATUS_FILTERS.map((value) => ({ value, label: value === 'completed' ? 'Completed' : runStatusView(value).label }));
  public readonly compact = input(false);
  public readonly sessions = input<Session[]>([]);
  public readonly liveSessionId = input<string | null>(null);
  public readonly selectedRunId = input<string | null>(null);
  public readonly liveStatus = input<string | null>(null);
  public readonly workspace = input(false);
  public readonly stopRun = output<string>();
  public readonly selectRun = output<string>();
  public readonly newRun = output<void>();
  public readonly runTitle = runTitle;
  public readonly refreshKey = input('');
  public readonly recordedDevices = input<ReadonlyMap<string, LabelableDevice>>(new Map());
  public readonly recordedImages = input<ReadonlyMap<string, GoalImage[]>>(new Map());
  public readonly mediaNotice = MEDIA_NOTICE;
  /** The run open beside this list on Runs, so its row can say so. */
  public readonly openRunId = toSignal(this.route.paramMap.pipe(map((params) => params.get('id'))), { initialValue: null });
  public readonly scope = signal<'mine' | 'everyone'>('mine');
  /**
   * Query for a row's link. Beside an open run the list keeps its own filters across rows, so
   * moving to another run neither resets the list nor loses the state "Back to runs" returns to.
   */
  public readonly runQuery = computed(() => {
    const review = this.scope() === 'everyone' ? { review: '1' } : {};
    return this.openRunId() ? { ...this.query(this.filters(), this.scope()), ...review } : this.scope() === 'everyone' ? { scope: 'everyone', ...review } : {};
  });

  /** What the URL says; the list always shows exactly this. */
  public readonly filters = signal<RunFilters>(EMPTY_FILTERS);
  /** The text in the search box, which may not be applied yet. */
  public readonly searchText = signal('');
  public readonly rows = signal<RunSummary[]>([]);
  public readonly nextCursor = signal<string | null>(null);
  public readonly loading = signal(true);
  public readonly loadingMore = signal(false);
  public readonly error = signal<'failed' | 'not_ready' | null>(null);
  public readonly computers = signal<Computer[]>([]);
  public readonly devices = signal<RegistryDevice[]>([]);

  public readonly queueRuns = computed(() => {
    const queue = new Map(this.rows().filter((run) => runStatusView(run.status).active).map((run) => [run.session_id, run]));
    if (this.scope() === 'mine' || this.workspace()) {
      for (const session of this.sessions()) {
        const status = sessionStatusView(session.status, session.session_id === this.liveSessionId() ? this.liveStatus() : null);
        if (!status.active) {
          queue.delete(session.session_id);
          continue;
        }
        const catalog = queue.get(session.session_id);
        queue.set(session.session_id, {
          ...catalog, session_id: session.session_id, prompt: session.initial_goal, status: status.key,
          interrupt_reason: catalog?.interrupt_reason ?? null, start_time: session.start_time, end_time: session.end_time ?? null,
          host_id: catalog?.host_id ?? null,
          device_ref: catalog?.device_ref ?? (session.device_serial ? { host_id: null, serial: session.device_serial } : null),
          requested_by: session.requested_by ?? null, pinned: catalog?.pinned ?? false, recordings: catalog?.recordings ?? []
        });
      }
    }
    return [...queue.values()].sort((first, second) =>
      Number(first.status === 'pending') - Number(second.status === 'pending') || (first.start_time ?? 0) - (second.start_time ?? 0));
  });

  public readonly dateGroups = computed(() => {
    const groups = new Map<string, { label: string; runs: RunSummary[] }>();
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);
    for (const run of this.rows()) {
      if (runStatusView(run.status).active) continue;
      const date = run.start_time === null ? null : new Date(run.start_time * 1000);
      const key = date?.toDateString() ?? 'unknown';
      const label = !date ? 'Date unknown' : key === today.toDateString() ? 'Today'
        : key === yesterday.toDateString() ? 'Yesterday'
        : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
      if (!groups.has(key)) groups.set(key, { label, runs: [] });
      groups.get(key)!.runs.push(run);
    }
    return [...groups.entries()].map(([key, group]) => ({ key, ...group }));
  });

  public readonly active = computed(() => hasActiveFilters(this.filters()));
  public readonly filterChips = computed(() => Object.entries(this.filters())
    .filter(([key, value]) => key !== 'q' && !!value)
    .map(([key, value]) => ({ key: key as keyof RunFilters, label: key === 'status'
      ? this.statusOptions.find((option) => option.value === value)?.label ?? value : `${key}: ${value}` })));
  public readonly moreOpen = computed(() => {
    const { device, host, requester } = this.filters();
    return !!(device || host || requester);
  });

  private readonly scrollRegion = viewChild<ElementRef<HTMLElement>>('scrollRegion');
  private readonly searchInput = viewChild<ElementRef<HTMLInputElement>>('searchInput');
  private request: Subscription | null = null;
  private pendingScroll = 0;
  private scrollTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFiltersKey: string | null = null;

  constructor() {
    this.hostsApi
      .list()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ next: (response) => {
        this.computers.set(response.hosts);
        this.devices.set(response.devices);
      }, error: () => undefined });

    this.route.queryParamMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((urlParams) => {
      // Beside an open run the URL carries no list state yet: start from the list the QA came from.
      const beside = !!this.openRunId();
      const params = beside && this.lastFiltersKey === null
        ? convertToParamMap({ ...this.runsApi.lastLibraryQuery(), ...Object.fromEntries(urlParams.keys.map((k) => [k, urlParams.get(k)!])) })
        : urlParams;
      const filters = filtersFromQuery(params);
      const scroll = scrollFromQuery(params);
      const scope = params.get('scope') === 'everyone' ? 'everyone' : 'mine';
      const key = JSON.stringify({ filters, scope });
      if (scope !== this.scope()) this.rows.set([]);
      this.scope.set(scope);
      // The list beside an open run must not overwrite what "Back to runs" returns to.
      if (!beside) this.runsApi.lastLibraryQuery.set({ ...this.query(filters), ...(scroll ? { scroll: String(scroll) } : {}) });
      if (key === this.lastFiltersKey) return; // only the scroll position changed
      // Restore the saved position on the first load only: a changed filter is a different list.
      this.pendingScroll = this.lastFiltersKey === null ? scroll : 0;
      this.lastFiltersKey = key;
      this.filters.set(filters);
      this.searchText.set(filters.q);
      this.load(false);
    });

    this.destroyRef.onDestroy(() => {
      this.request?.unsubscribe();
      if (this.scrollTimer) clearTimeout(this.scrollTimer);
    });
  }

  // -- search and filters -------------------------------------------------

  public ngOnChanges(changes: SimpleChanges): void {
    if (changes['refreshKey'] && !changes['refreshKey'].firstChange) this.load(false);
    const sessions = changes['sessions'];
    if (sessions && !sessions.firstChange && !changes['refreshKey']) {
      const finished = (items: Session[]) => JSON.stringify(items.filter((session) => !runStatusView(session.status).active)
        .map((session) => [session.session_id, session.status, session.end_time]));
      if (finished(sessions.previousValue) !== finished(sessions.currentValue)) this.load(false);
    }
  }

  public focusSearch(event: KeyboardEvent): void {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    if (event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])')) return;
    const search = this.searchInput()?.nativeElement;
    if (!search?.checkVisibility()) return;
    event.preventDefault();
    search.focus();
  }

  public submitSearch(event: Event): void {
    event.preventDefault();
    const intent = classifySearch(this.searchText());
    if (intent.kind === 'empty') return this.apply({ q: '' });
    if (intent.kind === 'text') return this.apply({ q: intent.q });
    this.runsApi
      .get(intent.id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (run) => void this.router.navigate(['/runs', run.session_id], { queryParams: this.runQuery() }),
        error: (error: HttpErrorResponse) => {
          // 409 and 410 are answers the viewer can explain; anything else may be a word that looks like an id.
          if (error.status === 409 || error.status === 410) void this.router.navigate(['/runs', intent.id], { queryParams: this.runQuery() });
          else this.apply({ q: intent.id });
        }
      });
  }

  public setFilter(key: keyof RunFilters, value: string): void {
    this.apply({ [key]: value } as Partial<RunFilters>);
  }

  public clearFilters(): void {
    this.apply(EMPTY_FILTERS);
  }

  public setScope(scope: 'mine' | 'everyone'): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: this.query(this.filters(), scope), replaceUrl: true });
  }

  public onTabKey(event: KeyboardEvent, scope: 'mine' | 'everyone'): void {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 'mine' : event.key === 'End' ? 'everyone' : scope === 'mine' ? 'everyone' : 'mine';
    this.setScope(next);
    const list = (event.currentTarget as HTMLElement).parentElement;
    list?.querySelector<HTMLButtonElement>(`[data-scope="${next}"]`)?.focus();
  }

  private query(filters = this.filters(), scope = this.scope()): Record<string, string> {
    return { ...filtersToQuery(filters), ...(scope === 'everyone' ? { scope } : {}) };
  }

  private apply(change: Partial<RunFilters>): void {
    const next = { ...this.filters(), ...change };
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: this.query(next),
      replaceUrl: true
    });
  }

  public onScroll(): void {
    if (this.scrollTimer) clearTimeout(this.scrollTimer);
    this.scrollTimer = setTimeout(() => {
      this.scrollTimer = null;
      const top = Math.floor(this.scrollRegion()?.nativeElement.scrollTop ?? 0);
      void this.router.navigate([], {
        relativeTo: this.route,
        queryParams: { ...this.query(), ...(top > 0 ? { scroll: top } : {}) },
        replaceUrl: true
      });
    }, 250);
  }

  /**
   * Opening a run can beat the scroll debounce. Save the position into this
   * history entry (and the remembered query) before the row's own navigation
   * leaves, so Back and "Back to runs" both land where the QA was.
   */
  public rememberPosition(): void {
    if (this.scrollTimer) clearTimeout(this.scrollTimer);
    this.scrollTimer = null;
    const top = Math.floor(this.scrollRegion()?.nativeElement.scrollTop ?? 0);
    const query = { ...this.query(), ...(top > 0 ? { scroll: String(top) } : {}) };
    // Beside an open run the saved scroll belongs to the page the QA came from, not to this list.
    if (this.openRunId()) {
      const { scroll: savedScroll, ...saved } = this.runsApi.lastLibraryQuery();
      const current = this.query();
      const same = JSON.stringify(saved) === JSON.stringify(current);
      this.runsApi.lastLibraryQuery.set({ ...current, ...(same && savedScroll ? { scroll: savedScroll } : {}) });
      return;
    }
    this.runsApi.lastLibraryQuery.set(query);
    const [path, search = ''] = this.router
      .serializeUrl(this.router.createUrlTree([], { relativeTo: this.route, queryParams: query }))
      .split('?');
    this.location.replaceState(path, search);
  }

  // -- loading --------------------------------------------------------------

  public retry(): void {
    this.load(false);
  }

  public loadMore(): void {
    // A cursor is only valid for the query that produced it; a reload drops it.
    if (this.loading() || this.loadingMore() || !this.nextCursor()) return;
    this.load(true);
  }

  private load(more: boolean): void {
    this.request?.unsubscribe();
    this.error.set(null);
    if (!more) this.nextCursor.set(null);
    (more ? this.loadingMore : this.loading).set(true);
    this.request = this.runsApi
      .list(this.filters(), { scope: this.scope(), ...(more ? { cursor: this.nextCursor() ?? undefined } : {}) })
      .subscribe({
        next: (page) => {
          this.rows.set(more ? [...this.rows(), ...page.runs] : page.runs);
          this.nextCursor.set(page.next_cursor);
          this.loading.set(false);
          this.loadingMore.set(false);
          this.restoreScroll();
        },
        error: (error: HttpErrorResponse) => {
          this.error.set(error.status === 503 ? 'not_ready' : 'failed');
          if (!more) this.rows.set([]);
          this.loading.set(false);
          this.loadingMore.set(false);
        }
      });
  }

  /** Back from a run: load pages until the saved position exists, then scroll to it. */
  private restoreScroll(): void {
    const target = this.pendingScroll;
    if (!target) return;
    setTimeout(() => {
      const region = this.scrollRegion()?.nativeElement;
      if (!region) return;
      if (region.scrollHeight - region.clientHeight >= target || !this.nextCursor()) {
        this.pendingScroll = 0;
        region.scrollTop = target;
      } else if (!this.loadingMore()) {
        this.loadMore();
      }
    });
  }

  // -- row presentation ---------------------------------------------------

  public computer(run: RunSummary): string {
    const source = this.devices().find((device) => device.serial === run.device_ref?.serial)?.computer_name;
    return source ?? (run.host_id === null ? 'A browser' : (this.computers().find((computer) => computer.id === run.host_id)?.name ?? 'Unknown computer'));
  }

  public device(run: RunSummary): LabelableDevice | null {
    return this.recordedDevices().get(run.session_id)
      ?? this.devices().find((device) => device.serial === run.device_ref?.serial) ?? null;
  }

  /** The open run's id may be a short prefix of the row's id. */
  public isOpen(run: RunSummary): boolean {
    const open = this.openRunId() ?? (this.workspace() ? this.selectedRunId() : null);
    return !!open && run.session_id.startsWith(open);
  }

  public trackRun(_: number, run: RunSummary): string {
    return run.session_id;
  }
}
