import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  viewChild
} from '@angular/core';
import { HttpErrorResponse, HttpEvent, HttpEventType } from '@angular/common/http';
import { DatePipe } from '@angular/common';
import { Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { RunSummary, SessionVideo, VideoSegment } from '../../core/models/run.model';
import { StepItemData } from '../../core/models/stream.model';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { RunsService } from '../../services/runs.service';
import {
  getActionObject,
  getActionTitle,
  getStepPostImageUrl,
  getStepPreImageUrl,
  isActionFailed
} from '../../utils/action-formatter.util';
import { Playback, mapRecording } from '../../utils/recording-state.util';
import { locateSessionTime } from '../../utils/recording-timeline.util';
import {
  DELETE_NOTICE,
  MEDIA_NOTICE,
  RUN_STRINGS,
  SHARE_NOTICE,
  UNPIN_EXPIRED_NOTICE,
  interruptReason,
  interruptedSentence,
  outcomeView,
  readOnlyText,
  removedReason
} from '../../utils/run-library-strings';
import { RunIdCopyComponent } from '../run-id-copy/run-id-copy.component';

type PageState = 'loading' | 'ready' | 'not_found' | 'removed' | 'access' | 'ambiguous' | 'error';
type DialogKind = 'share' | 'download' | 'unpin_expired' | 'delete';
type Retryable = 'download' | null;

const DIALOGS: Record<DialogKind, { title: string; notices: string[]; confirm: string }> = {
  share: { title: 'Copy link', notices: [MEDIA_NOTICE, SHARE_NOTICE], confirm: RUN_STRINGS.copyLink },
  download: { title: 'Download this run', notices: [MEDIA_NOTICE], confirm: RUN_STRINGS.download },
  unpin_expired: { title: 'Unpin this run?', notices: [UNPIN_EXPIRED_NOTICE], confirm: RUN_STRINGS.unpin },
  delete: { title: 'Delete this run?', notices: [DELETE_NOTICE], confirm: RUN_STRINGS.delete }
};

@Component({
  selector: 'app-run-viewer',
  standalone: true,
  imports: [RouterLink, DatePipe, RunIdCopyComponent],
  templateUrl: './run-viewer.component.html',
  styleUrl: './run-viewer.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class RunViewerComponent {
  private readonly runsApi = inject(RunsService);
  private readonly ownerScope = inject(OwnerScopeService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  /** Full run id or its 8-character prefix, from the URL. */
  public readonly runId = input.required<string>();

  public readonly strings = RUN_STRINGS;
  public readonly outcome = outcomeView;
  public readonly interruptReason = interruptReason;
  public readonly removedReason = removedReason;

  public readonly state = signal<PageState>('loading');
  public readonly run = signal<RunSummary | null>(null);
  public readonly removed = signal<{ reason?: string } | null>(null);
  public readonly candidates = signal<string[]>([]);
  public readonly steps = signal<StepItemData[]>([]);
  public readonly stepsLoaded = signal(false);
  public readonly stepsFailed = signal(false);
  public readonly video = signal<SessionVideo | null>(null);
  public readonly videoFailed = signal(false);
  /** The media element itself could not load or decode the file. */
  public readonly playerFailed = signal(false);
  public readonly selectedStepId = signal<string | null>(null);
  public readonly activeSegmentIndex = signal(0);
  public readonly compact = signal(false);
  public readonly techOpen = signal(false);

  public readonly dialogKind = signal<DialogKind | null>(null);
  public readonly feedback = signal('');
  public readonly actionError = signal<{ text: string; retry: Retryable } | null>(null);

  /** Pin and Delete change the run, so only its owner or an admin gets them; the server checks too. */
  public readonly canChange = computed(() => this.ownerScope.canAct(this.run()?.requested_by));
  /** Set once we know who is looking and they may not change this run. */
  public readonly readOnlyNote = computed(() =>
    this.ownerScope.identity() && this.run() && !this.canChange() ? readOnlyText(this.run()!.requested_by) : null
  );

  public readonly lastQuery = this.runsApi.lastLibraryQuery;
  public readonly dialog = computed(() => (this.dialogKind() ? DIALOGS[this.dialogKind()!] : null));

  public readonly segments = computed(() => {
    let start = 0;
    return (this.video()?.video_segments ?? []).map((segment: VideoSegment) => {
      const withStart = { ...segment, start: segment.start ?? start };
      start = withStart.start + segment.duration;
      return withStart as VideoSegment & { start: number };
    });
  });

  public readonly selectedStep = computed(() => {
    const steps = this.steps();
    return steps.find((s) => s.step_id === this.selectedStepId()) ?? steps[steps.length - 1] ?? null;
  });

  public readonly recording = computed(() => {
    const first = this.run()?.recordings[0];
    const segments = this.segments();
    const stoppedAt = segments.length ? segments.reduce((sum, s) => sum + s.duration, 0) : null;
    return mapRecording(
      {
        capture: (first?.capture ?? null) as never,
        transfer: (first?.transfer ?? null) as never,
        playback: (this.video()?.status ?? null) as Playback
      },
      { stoppedAtSeconds: stoppedAt }
    );
  });

  /** Player-area copy; a failed check or a file the browser cannot play wins over the mapped copy. */
  public readonly copy = computed(() => {
    if (this.videoFailed()) return "Couldn't check the video.";
    if (this.playerFailed()) return 'The video could not be played. Steps and screenshots are still here.';
    return this.recording().copy;
  });

  public readonly videoUrl = computed(
    () => this.segments()[this.activeSegmentIndex()]?.url ?? this.video()?.video_url ?? null
  );

  public readonly screenshot = computed(() => {
    const step = this.selectedStep();
    return step ? (getStepPostImageUrl(step) ?? getStepPreImageUrl(step)) : null;
  });

  public readonly interruptedAtStep = computed(() => this.steps()[this.steps().length - 1]?.step_number ?? null);
  public readonly interruptedText = computed(() => interruptedSentence(this.interruptedAtStep()));
  public readonly canCheckAgain = computed(
    () =>
      this.videoFailed() ||
      this.playerFailed() ||
      ['preparing', 'uploaded_unchecked', 'prepare_failed'].includes(this.recording().state)
  );
  public readonly rawLogs = computed(() => JSON.stringify({ run: this.run(), steps: this.steps() }, null, 2));

  private readonly dialogEl = viewChild<ElementRef<HTMLDialogElement>>('dialogEl');
  private readonly playerEl = viewChild<ElementRef<HTMLVideoElement>>('playerEl');
  private opener: HTMLElement | null = null;
  private loadRequest: Subscription | null = null;
  private evidenceRequests = new Subscription();
  /** Pin, download and delete: cancelled on navigation so run A's answer never lands on run B. */
  private actionRequests = new Subscription();
  private pendingSeek: number | null = null;
  private continuePlaying = false;

  constructor() {
    this.ownerScope.load();

    const query = typeof window !== 'undefined' ? window.matchMedia('(max-width: 1279px)') : null;
    if (query) {
      this.compact.set(query.matches);
      const onChange = (event: MediaQueryListEvent) => this.compact.set(event.matches);
      query.addEventListener('change', onChange);
      this.destroyRef.onDestroy(() => query.removeEventListener('change', onChange));
    }

    effect(() => {
      const id = this.runId();
      untracked(() => this.load(id));
    });
    this.destroyRef.onDestroy(() => {
      this.loadRequest?.unsubscribe();
      this.evidenceRequests.unsubscribe();
      this.actionRequests.unsubscribe();
    });
  }

  // -- loading ----------------------------------------------------------------

  public reload(): void {
    this.load(this.runId());
  }

  private load(id: string): void {
    this.loadRequest?.unsubscribe();
    this.evidenceRequests.unsubscribe(); // a slow answer for the previous run must not land on this one
    this.evidenceRequests = new Subscription();
    this.actionRequests.unsubscribe();
    this.actionRequests = new Subscription();
    this.playerFailed.set(false);
    this.state.set('loading');
    this.run.set(null);
    this.steps.set([]);
    this.stepsLoaded.set(false);
    this.video.set(null);
    this.selectedStepId.set(null);
    this.activeSegmentIndex.set(0);
    this.feedback.set('');
    this.actionError.set(null);
    this.loadRequest = this.runsApi.get(id).subscribe({
      next: (run) => {
        this.run.set(run);
        this.state.set('ready');
        this.loadEvidence(run.session_id);
      },
      error: (error: HttpErrorResponse) => this.fail(error)
    });
  }

  private fail(error: HttpErrorResponse): void {
    const body = (error.error ?? {}) as { reason?: string; candidates?: string[] };
    switch (error.status) {
      case 400: // invalid_session_id: this id can never resolve, so Retry would be a lie
      case 404:
        return this.state.set('not_found');
      case 410:
        this.removed.set({ reason: body.reason });
        return this.state.set('removed');
      case 409:
        this.candidates.set(body.candidates ?? []);
        return this.state.set('ambiguous');
      case 401:
      case 403:
      case 0: // Cloudflare Access answers an expired session with a cross-origin redirect
        return this.state.set('access');
      default:
        return this.state.set('error');
    }
  }

  private loadEvidence(sessionId: string): void {
    this.stepsFailed.set(false);
    this.evidenceRequests.add(
      this.runsApi.steps(sessionId).subscribe({
        next: (steps) => {
          this.steps.set(steps);
          this.stepsLoaded.set(true);
        },
        error: () => {
          this.stepsFailed.set(true);
          this.stepsLoaded.set(true);
        }
      })
    );
    this.loadVideo(sessionId);
  }

  private loadVideo(sessionId: string): void {
    this.videoFailed.set(false);
    this.evidenceRequests.add(
      this.runsApi.video(sessionId).subscribe({
        next: (video) => this.video.set(video),
        error: () => this.videoFailed.set(true)
      })
    );
  }

  public checkAgain(): void {
    const id = this.run()?.session_id;
    if (!id) return;
    this.playerFailed.set(false);
    this.loadVideo(id);
  }

  // -- steps and playback -----------------------------------------------------

  public stepTitle(step: StepItemData): string {
    return getActionTitle(step.action_taken);
  }

  public stepFailed(step: StepItemData): boolean {
    return isActionFailed(getActionObject(step.action_taken), step);
  }

  public selectStep(step: StepItemData): void {
    this.selectedStepId.set(step.step_id);
    const run = this.run();
    if (!this.recording().playable) return;
    const sessionSeconds = step.timestamp - (run?.start_time ?? step.timestamp);
    if (!this.segments().length) {
      // One file, no manifest: the session axis is the file's own axis.
      this.pendingSeek = Math.max(0, sessionSeconds);
      return this.applySeek();
    }
    const located = locateSessionTime(this.segments(), sessionSeconds);
    if (!located) return;
    this.pendingSeek = located.localTime;
    if (located.index === this.activeSegmentIndex()) this.applySeek();
    else this.activeSegmentIndex.set(located.index);
  }

  public onMetadata(): void {
    this.applySeek();
    if (this.continuePlaying) {
      this.continuePlaying = false;
      void this.playerEl()?.nativeElement.play().catch(() => undefined);
    }
  }

  private applySeek(): void {
    const player = this.playerEl()?.nativeElement;
    if (player && this.pendingSeek !== null && player.readyState > 0) {
      const end = Number.isFinite(player.duration) ? player.duration : this.pendingSeek;
      player.currentTime = Math.min(this.pendingSeek, end);
      this.pendingSeek = null;
    }
  }

  public onEnded(): void {
    if (this.activeSegmentIndex() < this.segments().length - 1) {
      this.continuePlaying = true;
      this.activeSegmentIndex.update((index) => index + 1);
    }
  }

  // -- actions ----------------------------------------------------------------

  public ask(kind: DialogKind, event?: Event): void {
    this.opener = (event?.currentTarget as HTMLElement | null) ?? null;
    this.dialogKind.set(kind);
    this.dialogEl()?.nativeElement.showModal();
  }

  public closeDialog(): void {
    const dialog = this.dialogEl()?.nativeElement;
    if (dialog?.open) dialog.close();
    this.dialogKind.set(null);
    this.opener?.focus();
  }

  public onDialogCancel(event: Event): void {
    event.preventDefault();
    this.closeDialog();
  }

  public confirm(): void {
    const kind = this.dialogKind();
    this.closeDialog();
    if (kind === 'share') void this.copyLink();
    else if (kind === 'download') this.download();
    else if (kind === 'unpin_expired') this.setPinned(false);
    else if (kind === 'delete') this.remove();
  }

  public togglePin(event: Event): void {
    const run = this.run();
    if (!run) return;
    if (!run.pinned) return this.setPinned(true);
    if (run.expires_at != null && run.expires_at <= Date.now() / 1000) return this.ask('unpin_expired', event);
    this.setPinned(false);
  }

  private setPinned(pinned: boolean): void {
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    this.actionRequests.add(
      (pinned ? this.runsApi.pin(run.session_id) : this.runsApi.unpin(run.session_id)).subscribe({
        next: () => this.run.set({ ...run, pinned }),
        error: () => this.actionError.set({ text: RUN_STRINGS.pinFailed, retry: null })
      })
    );
  }

  private async copyLink(): Promise<void> {
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    try {
      await navigator.clipboard.writeText(`${window.location.origin}/runs/${run.session_id}`);
      if (this.run()?.session_id === run.session_id) this.feedback.set(RUN_STRINGS.linkCopied);
    } catch {
      if (this.run()?.session_id === run.session_id) this.actionError.set({ text: "Couldn't copy the link. Copy it from the address bar.", retry: null });
    }
  }

  public download(): void {
    const run = this.run();
    if (!run) return;
    this.actionError.set(null);
    this.feedback.set(RUN_STRINGS.preparingBundle);
    this.actionRequests.add(
      this.runsApi.downloadBundle(run.session_id).subscribe({
        next: (event) => this.onBundleEvent(event, run.session_id),
        error: () => {
          this.feedback.set('');
          this.actionError.set({ text: RUN_STRINGS.downloadFailed, retry: 'download' });
        }
      })
    );
  }

  public retry(): void {
    if (this.actionError()?.retry === 'download') this.download();
  }

  private onBundleEvent(event: HttpEvent<Blob>, sessionId: string): void {
    if (event.type === HttpEventType.ResponseHeader) {
      const bytes = Number(event.headers.get('Content-Length'));
      if (bytes > 0) this.feedback.set(`Downloading bundle (${Math.max(1, Math.round(bytes / 1048576))} MB)…`);
    } else if (event.type === HttpEventType.Response && event.body) {
      // ponytail: the bundle (1 GiB cap) is held as a blob so a failure can offer Retry; stream via an anchor if that hurts.
      const url = URL.createObjectURL(event.body);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `run-${sessionId.slice(0, 8)}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
      this.feedback.set(RUN_STRINGS.downloadStarted);
    }
  }

  private remove(): void {
    const run = this.run();
    if (!run) return;
    this.actionRequests.add(
      this.runsApi.remove(run.session_id).subscribe({
        next: () => void this.router.navigate(['/runs'], { queryParams: this.lastQuery() }),
        error: () => this.actionError.set({ text: RUN_STRINGS.deleteFailed, retry: null })
      })
    );
  }

  public startNewRun(): void {
    void this.router.navigate(['/workspace'], { state: { draftPrompt: this.run()?.prompt ?? '' } });
  }

  public trackStep(_: number, step: StepItemData): string {
    return step.step_id;
  }
}
