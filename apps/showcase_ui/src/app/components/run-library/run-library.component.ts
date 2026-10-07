import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  inject,
  signal,
  viewChild
} from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DatePipe, Location } from '@angular/common';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { Computer } from '../../core/models/host.model';
import { RunSummary } from '../../core/models/run.model';
import { HostsService } from '../../services/hosts.service';
import { RunsService } from '../../services/runs.service';
import { mapRecording } from '../../utils/recording-state.util';
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
  expiresText,
  interruptReason,
  truncate
} from '../../utils/run-library-strings';
import { serialShape, unlistedRunDeviceTitle } from '../../utils/device-label.util';
import { runStatusView } from '../../utils/run-status.util';
import { classifySearch } from '../../utils/run-search.util';

@Component({
  selector: 'app-run-library',
  standalone: true,
  imports: [RouterLink, DatePipe],
  templateUrl: './run-library.component.html',
  styleUrl: './run-library.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunLibraryComponent {
  private readonly runsApi = inject(RunsService);
  private readonly hostsApi = inject(HostsService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly location = inject(Location);
  private readonly destroyRef = inject(DestroyRef);

  public readonly strings = RUN_STRINGS;
  public readonly statusOptions = STATUS_FILTERS.map((value) => ({ value, label: runStatusView(value).label }));
  public readonly outcome = runStatusView;
  public readonly interruptReason = interruptReason;

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

  public readonly active = computed(() => hasActiveFilters(this.filters()));
  public readonly moreOpen = computed(() => {
    const { device, host, requester } = this.filters();
    return !!(device || host || requester);
  });
  public readonly nowSeconds = Math.floor(Date.now() / 1000);

  private readonly scrollRegion = viewChild<ElementRef<HTMLElement>>('scrollRegion');
  private request: Subscription | null = null;
  private pendingScroll = 0;
  private scrollTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFiltersKey: string | null = null;

  constructor() {
    this.hostsApi
      .list()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ next: (response) => this.computers.set(response.hosts), error: () => undefined });

    this.route.queryParamMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const filters = filtersFromQuery(params);
      const scroll = scrollFromQuery(params);
      const key = JSON.stringify(filters);
      this.runsApi.lastLibraryQuery.set({ ...filtersToQuery(filters), ...(scroll ? { scroll: String(scroll) } : {}) });
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

  public submitSearch(event: Event): void {
    event.preventDefault();
    const intent = classifySearch(this.searchText());
    if (intent.kind === 'empty') return this.apply({ q: '' });
    if (intent.kind === 'text') return this.apply({ q: intent.q });
    this.runsApi
      .get(intent.id)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (run) => void this.router.navigate(['/runs', run.session_id]),
        error: (error: HttpErrorResponse) => {
          // 409 and 410 are answers the viewer can explain; anything else may be a word that looks like an id.
          if (error.status === 409 || error.status === 410) void this.router.navigate(['/runs', intent.id]);
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

  private apply(change: Partial<RunFilters>): void {
    const next = { ...this.filters(), ...change };
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: filtersToQuery(next),
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
        queryParams: { ...filtersToQuery(this.filters()), ...(top > 0 ? { scroll: top } : {}) },
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
    const query = { ...filtersToQuery(this.filters()), ...(top > 0 ? { scroll: String(top) } : {}) };
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
      .list(this.filters(), more ? { cursor: this.nextCursor() ?? undefined } : undefined)
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

  public prompt(run: RunSummary): string {
    return truncate(run.prompt);
  }

  public recordingBadge(run: RunSummary): string {
    const recording = run.recordings[0];
    return mapRecording({
      capture: (recording?.capture ?? null) as never,
      transfer: (recording?.transfer ?? null) as never,
      playback: null
    }).badge;
  }

  public device(run: RunSummary): string {
    // The address is detail (the row's tooltip); a browser-relayed phone is named by the computer part.
    const serial = run.device_ref?.serial;
    const phone = !serial ? 'Unknown phone' : serialShape(serial) === 'loopback' ? 'Phone' : unlistedRunDeviceTitle(serial, false);
    const computer =
      run.host_id === null
        ? 'A browser'
        : (this.computers().find((c) => c.id === run.host_id)?.name ?? 'Unknown computer');
    return `${phone} · ${computer}`;
  }

  public expires(run: RunSummary): string | null {
    return expiresText(run.expires_at, this.nowSeconds);
  }

  public trackRun(_: number, run: RunSummary): string {
    return run.session_id;
  }
}
