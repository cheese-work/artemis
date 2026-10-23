import { DatePipe } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, NgZone, signal } from '@angular/core';
import { catchError, forkJoin, map, of, switchMap, takeWhile, timer } from 'rxjs';
import { RunSummary, SessionVideo } from '../../core/models/run.model';
import { SessionUsage } from '../../core/models/session.model';
import { RunsService } from '../../services/runs.service';
import { appUrl, mediaUrl } from '../../utils/app-url.util';

const formatCount = (count: number | undefined) => (count ?? 0).toLocaleString('en-US');
const formatCost = (usd: number) => usd > 0 && usd < 0.01 ? '< $0.01' : (usd === 0 ? '$0.00' : '≈ $' + usd.toFixed(2));

@Component({
  selector: 'app-session-sidebar',
  standalone: true,
  imports: [DatePipe],
  host: { '[class.empty-media]': 'emptyMediaPanel()' },
  template: `
    <aside aria-label="Session sidebar">
      <div class="tabs" role="tablist" aria-label="Session evidence">
        @for (name of tabs; track name) {
          <button type="button" role="tab" [id]="'session-tab-' + name" [attr.aria-selected]="tab() === name"
            [attr.tabindex]="tab() === name ? 0 : -1" aria-controls="session-sidebar-panel"
            (click)="tab.set(name)" (keydown)="tabKey($event, name)">{{ name }}</button>
        }
      </div>
      <section id="session-sidebar-panel" role="tabpanel" [attr.aria-labelledby]="'session-tab-' + tab()">
        @if (!runId()) { <p class="muted">Device evidence appears when a session starts.</p> }
        @else if (tab() === 'Live') {
          @if (active()) {
            @if (liveUrl(); as url) { <img class="device-feed" [src]="url" alt="Live screen of the session phone" (error)="streamFailed.set(true)"> }
            @else { <p class="muted" role="status">Live feed unavailable for this session phone. The timeline still updates.</p> }
          } @else if (videoUrl(); as url) {
            <video controls playsinline preload="metadata" [src]="url" aria-label="Session recording" (error)="videoFailed.set(true)"></video>
          } @else {
            <p class="muted" role="status">{{ videoFailed() ? 'Recording could not be played.' : video()?.status === 'processing' ? 'Recording is processing.' : video()?.message || 'No recording is available.' }}</p>
          }
        } @else if (tab() === 'Details') {
          <dl>
            <dt>App</dt><dd>{{ run()?.app_package || 'Not recorded' }}</dd>
            <dt>Phone</dt><dd>{{ run()?.device_ref?.serial || 'Not recorded' }}</dd>
            <dt>Model</dt><dd>{{ run()?.agent_model || 'Not recorded' }}</dd>
            @if (technicalDetails()) {
            <dt>Started</dt><dd>{{ run()?.start_time ? (run()!.start_time! * 1000 | date: 'MMM d, y, HH:mm' : '+0700') + ' ICT' : 'Not recorded' }}</dd>
            <dt>Elapsed</dt><dd>{{ elapsed() }} seconds</dd>
            <dt>Run ID</dt><dd class="mono">{{ runId() }}</dd>
            }
            <dt>First prompt</dt><dd class="prompt">{{ firstPrompt() || 'Not recorded' }}</dd>
          </dl>
        } @else {
          <dl>
            <dt>Steps</dt><dd>{{ steps() ?? 'Unavailable' }}</dd>
            @if (technicalDetails()) { <dt>Duration</dt><dd>{{ elapsed() }} seconds</dd> }
          </dl>
          @if (stats(); as totals) {
            <div class="stats" aria-label="Session usage">
              <div class="big"><span><b>{{ totals.tokens }}</b> tokens</span><span><b>{{ totals.cost ?? '—' }}</b> {{ totals.cost ? 'est.' : 'cost' }}</span></div>
              <table>
                <tr><td>Input · Output</td><td class="r">{{ totals.input }} · {{ totals.output }}</td></tr>
                @for (turn of totals.turns; track turn.number) {
                  <tr><td>{{ 'Turn ' + turn.number + ' · ' + turn.model }}</td><td class="r">{{ turn.tokens }} · {{ turn.cost ?? '—' }}</td></tr>
                }
                <tr><td colspan="2" class="faint">{{ totals.cost ? 'Estimate at list price; your provider bill may differ.' : 'No list price for this model.' }}</td></tr>
              </table>
            </div>
          } @else { <p class="muted">Usage unavailable.</p> }
        }
        @if (error()) { <p role="alert">{{ error() }}</p> }
      </section>
    </aside>
  `,
  styles: `
    :host { display: block; height: 100%; min-width: 0; border-left: 1px solid var(--color-rule); background: var(--color-surface); }
    :host(.empty-media) section { flex: 0 0 0; height: 0; overflow: hidden; padding: 0; }
    :host(.empty-media) section .muted { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
    aside { display: flex; flex-direction: column; height: 100%; }
    .tabs { display: flex; flex: none; border-bottom: 1px solid var(--color-rule); }
    button { flex: 1; min-width: 44px; min-height: 44px; border: 0; border-bottom: 3px solid transparent; background: var(--color-surface); color: var(--color-text-muted); font: inherit; cursor: pointer; }
    button[aria-selected=true] { border-bottom-color: var(--color-primary); color: var(--color-text); }
    button:focus-visible { outline: 2px solid var(--color-focus); outline-offset: -2px; }
    section { flex: 1; min-height: 0; overflow: auto; padding: var(--space-md); }
    .device-feed, video { display: block; width: 100%; max-height: 70dvh; object-fit: contain; border-radius: var(--radius-md); }
    section:has(.device-feed), section:has(video) { display: flex; align-items: flex-start; }
    section .device-feed, section video { max-height: 100%; }
    dl { margin: 0; } dt { margin-top: var(--space-md); color: var(--color-text-muted); font-size: var(--text-label); }
    dd { margin: var(--space-xs) 0 0; overflow-wrap: anywhere; } .prompt { white-space: pre-wrap; } .mono { font-family: var(--font-mono); }
    .muted { color: var(--color-text-muted); font-size: var(--text-ui); }
    .stats { display: grid; gap: var(--space-xs); margin-top: var(--space-md); padding-top: var(--space-md); border-top: 1px solid var(--color-rule); }
    .stats .big { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-md); color: var(--color-text-muted); font-size: var(--text-label); }
    .stats .big b { font-family: var(--font-mono); font-size: var(--text-section); color: var(--color-ink); font-weight: 500; }
    .stats table { width: 100%; border-collapse: collapse; font-size: var(--text-label); }
    .stats td { padding: 2px 0; color: var(--color-text-muted); overflow-wrap: anywhere; }
    .stats td.r { text-align: right; font-family: var(--font-mono); color: var(--color-text); white-space: nowrap; padding-left: var(--space-sm); }
    .stats td.faint { color: var(--color-text-faint); }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class SessionSidebarComponent {
  private readonly api = inject(RunsService);
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  public readonly runId = input('');
  public readonly technicalDetails = input(false);
  public readonly firstPrompt = input<string | null>(null);
  /** Every turn of the session, so Stats totals the whole session; defaults to the viewed run alone. */
  public readonly turns = input<readonly { id: string; number: number }[]>([]);
  public readonly tabs = ['Live', 'Details', 'Stats'] as const;
  public readonly tab = signal<(typeof this.tabs)[number]>('Live');
  public readonly run = signal<RunSummary | null>(null);
  public readonly usage = signal<{ number: number; usage: SessionUsage }[] | null>(null);
  private readonly turnKey = computed(() => this.turns().map(turn => turn.id + ':' + turn.number).join(','));
  public readonly stats = computed(() => {
    const entries = this.usage();
    if (!entries?.length) return null;
    const sum = (pick: (u: SessionUsage) => number | undefined) => entries.reduce((total, entry) => total + (pick(entry.usage) ?? 0), 0);
    const costs = entries.map(entry => entry.usage.estimated_cost_usd ?? null);
    return {
      tokens: formatCount(sum(u => u.total_tokens)),
      input: formatCount(sum(u => u.prompt_tokens)),
      output: formatCount(sum(u => u.completion_tokens)),
      cost: costs.every(cost => cost !== null) ? formatCost(costs.reduce<number>((total, cost) => total + (cost ?? 0), 0)) : null,
      turns: entries.map(entry => ({
        number: entry.number,
        model: entry.usage.model ?? 'Unknown model',
        tokens: formatCount(entry.usage.total_tokens),
        cost: entry.usage.estimated_cost_usd == null ? null : formatCost(entry.usage.estimated_cost_usd)
      }))
    };
  });
  public readonly steps = signal<number | null>(null);
  public readonly video = signal<SessionVideo | null>(null);
  public readonly error = signal<string | null>(null);
  public readonly streamFailed = signal(false);
  public readonly videoFailed = signal(false);
  private readonly now = signal(Date.now() / 1000);
  private readonly stream = signal<{ serial: string | null; live_stream_url: string | null } | null>(null);
  public readonly active = computed(() => ['queued', 'pending', 'starting', 'running', 'paused'].includes(this.run()?.status ?? ''));
  public readonly elapsed = computed(() => Math.max(0, Math.floor((this.run()?.end_time ?? this.now()) - (this.run()?.start_time ?? this.now()))));
  public readonly liveUrl = computed(() => {
    const stream = this.stream();
    const serial = this.run()?.device_ref?.serial;
    return !this.streamFailed() && serial && stream?.serial === serial && stream.live_stream_url
      ? appUrl(stream.live_stream_url + '?serial=' + encodeURIComponent(serial)) : null;
  });
  public readonly videoUrl = computed(() => !this.videoFailed() ? mediaUrl(this.video()?.video_url ?? this.video()?.video_segments[0]?.url) : null);
  public readonly emptyMediaPanel = computed(() => !!this.runId() && !this.error() && this.tab() === 'Live' && (this.active() ? !this.liveUrl() && !this.streamFailed() : !this.videoUrl() && !this.videoFailed() && this.video()?.status !== 'processing'));

  constructor() {
    effect(onCleanup => {
      const id = this.runId();
      this.turnKey();
      this.run.set(null);
      this.video.set(null);
      this.usage.set(null);
      this.steps.set(null);
      this.stream.set(null);
      this.streamFailed.set(false);
      this.videoFailed.set(false);
      this.error.set(null);
      if (!id) return;
      const usageTurns = this.turns().length ? this.turns() : [{ id, number: 1 }];
      const subscription = this.zone.runOutsideAngular(() => timer(0, 2000).pipe(switchMap(() => forkJoin({
        run: this.api.get(id).pipe(catchError(() => { this.error.set('Session details could not load.'); return of(null); })),
        video: this.api.video(id).pipe(catchError(() => of(null))),
        steps: this.api.steps(id).pipe(catchError(() => of(null))),
        usage: forkJoin(usageTurns.map(turn => this.http.get<SessionUsage>(
          '/api/sessions/' + encodeURIComponent(turn.id) + '/usage'
        ).pipe(catchError(() => of(null)), map(usage => usage && { number: turn.number, usage })))),
        stream: this.http.get<{ serial: string | null; live_stream_url: string | null }>(
          '/api/stream/device-state'
        ).pipe(catchError(() => of(null)))
      })), takeWhile(result => !result.run || ['queued', 'pending', 'starting', 'running', 'paused'].includes(result.run.status), true)).subscribe(result => this.zone.run(() => {
        this.now.set(Date.now() / 1000);
        this.run.set(result.run);
        this.video.set(result.video);
        this.steps.set(result.steps?.length ?? null);
        const usage = result.usage.filter(entry => entry !== null);
        this.usage.set(usage.length === result.usage.length ? usage : null);
        this.stream.set(result.stream);
        if (result.run) this.error.set(null);
      })));
      onCleanup(() => subscription.unsubscribe());
    });
  }

  public tabKey(event: KeyboardEvent, name: (typeof this.tabs)[number]): void {
    let index = this.tabs.indexOf(name);
    if (event.key === 'ArrowRight') index = (index + 1) % this.tabs.length;
    else if (event.key === 'ArrowLeft') index = (index + this.tabs.length - 1) % this.tabs.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = this.tabs.length - 1;
    else return;
    event.preventDefault();
    this.tab.set(this.tabs[index]);
    (event.currentTarget as HTMLElement).parentElement?.querySelectorAll<HTMLButtonElement>('button')[index]?.focus();
  }
}
